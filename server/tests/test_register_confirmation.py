import hashlib
import logging
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app import app
from server.db import create_db_engine
from server.models import accounts, confirmation_links, sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
REJECTED_PASSWORD = "abcdefgh1"
LOGGER = "server.confirmation"


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(PROJECT_ROOT),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def database() -> None:
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
    assert up.returncode == 0, up.stderr + up.stdout
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout
    try:
        yield
    finally:
        _compose("down", "--remove-orphans", timeout=60)


def _post(client: TestClient, path: str, payload: object, origin: str | None = ORIGIN):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    return client.post(path, json=payload, headers=headers)


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _link_ids() -> set[uuid.UUID]:
    return {row["id"] for row in _rows(confirmation_links)}


def _captured(caplog: pytest.LogCaptureFixture) -> str:
    captured = caplog.text
    for record in caplog.records:
        captured += record.getMessage()
        captured += repr(record.args)
    return captured


def _confirmation_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


def test_register_stores_one_link_and_logs_its_url(
    database: None, caplog: pytest.LogCaptureFixture
) -> None:
    raw_email = "  User Name@Example.COM  "
    stored_email = "user name@example.com"
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            response = _post(
                client,
                "/api/register",
                {"email": raw_email, "password": ACCEPTED},
            )
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            me = client.get("/api/me")
    body = response.json()
    assert body["email_confirmed"] is False
    assert body["period"] is None
    assert body["quota"] is None
    assert me.status_code == 200
    assert me.json()["email_confirmed"] is False
    assert me.json()["period"] is None
    assert me.json()["quota"] is None
    assert me.json()["account"]["email"] == stored_email

    account_id = uuid.UUID(body["account"]["id"])
    links = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(links) == 1
    link = links[0]
    assert tuple(link) == (
        "id",
        "account_id",
        "token_hash",
        "expires_at",
        "revoked_at",
    )
    assert link["revoked_at"] is None
    assert link["expires_at"].tzinfo is not None
    assert link["expires_at"].utcoffset() == timedelta(0)
    deadline = datetime.now(timezone.utc) + timedelta(hours=24)
    assert abs((link["expires_at"] - deadline).total_seconds()) < 10

    records = _confirmation_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    url, logged_email = message.split(" ", 1)
    assert logged_email == stored_email
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.netloc == "testserver"
    assert parsed.path == "/api/confirm-email"
    assert list(parse_qs(parsed.query)) == ["token"]
    token = parse_qs(parsed.query)["token"][0]
    assert message == f"http://testserver/api/confirm-email?token={token} {stored_email}"
    assert message.count(token) == 1
    assert link["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert link["token_hash"] != token
    stored = [link]
    stored.extend(row for row in _rows(accounts) if row["id"] == account_id)
    stored.extend(row for row in _rows(sessions) if row["account_id"] == account_id)
    visible = "\n".join(str(value) for row in stored for value in row.values())
    assert token not in visible
    assert token not in response.text
    assert token not in cookie
    assert ACCEPTED not in _captured(caplog)
    for record in caplog.records:
        if record.name == LOGGER:
            continue
        assert token not in record.getMessage() + repr(record.args)


def test_failed_register_does_not_store_a_link_or_log_the_password(
    database: None, caplog: pytest.LogCaptureFixture
) -> None:
    before = _link_ids()
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            missing_origin = _post(
                client,
                "/api/register",
                {"email": "origin-miss@example.com", "password": ACCEPTED},
                origin=None,
            )
            foreign_origin = _post(
                client,
                "/api/register",
                {"email": "origin-bad@example.com", "password": ACCEPTED},
                origin="http://evil.example",
            )
            bad_password = _post(
                client,
                "/api/register",
                {"email": "bad-password@example.com", "password": REJECTED_PASSWORD},
            )
            invalid_body = _post(
                client,
                "/api/register",
                {"email": "invalid-body@example.com"},
            )
            not_strings = _post(
                client,
                "/api/register",
                {"email": "not-strings@example.com", "password": 1},
            )
            not_object = _post(
                client,
                "/api/register",
                ["not-object@example.com", ACCEPTED],
            )
    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403
    assert bad_password.status_code == 400
    assert invalid_body.status_code == 400
    assert not_strings.status_code == 400
    assert not_object.status_code == 400
    assert _link_ids() == before
    assert _confirmation_records(caplog) == []
    captured = _captured(caplog)
    assert ACCEPTED not in captured
    assert REJECTED_PASSWORD not in captured

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            created = _post(
                client,
                "/api/register",
                {"email": "  Taken Link@Example.COM ", "password": ACCEPTED},
            )
            assert created.status_code == 200
            caplog.clear()
            taken = _post(
                client,
                "/api/register",
                {"email": "taken link@example.com", "password": ACCEPTED},
            )
    assert taken.status_code == 400
    assert taken.json()["code"] == "email_taken"
    account_id = uuid.UUID(created.json()["account"]["id"])
    owned = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(owned) == 1
    assert _confirmation_records(caplog) == []
    assert ACCEPTED not in _captured(caplog)


def test_login_does_not_issue_a_confirmation_link(
    database: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            registered = _post(
                client,
                "/api/register",
                {"email": "  Login Link@Example.COM ", "password": ACCEPTED},
            )
            assert registered.status_code == 200
            caplog.clear()
            logged_in = _post(
                client,
                "/api/login",
                {"email": "login link@example.com", "password": ACCEPTED},
            )
    assert logged_in.status_code == 200
    assert logged_in.json()["email_confirmed"] is False
    assert logged_in.json()["period"] is None
    assert logged_in.json()["quota"] is None
    account_id = uuid.UUID(registered.json()["account"]["id"])
    owned = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(owned) == 1
    assert _confirmation_records(caplog) == []
    assert ACCEPTED not in _captured(caplog)
