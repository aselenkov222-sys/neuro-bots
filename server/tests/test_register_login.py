import logging
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import create_account
from server.app import app
from server.db import create_db_engine
from server.models import accounts, sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
OTHER = "abcdef2!"
TAKEN = {
    "code": "email_taken",
    "message": "Эта почта уже зарегистрирована",
}
INVALID_PASSWORD = {
    "code": "invalid_password",
    "message": "Пароль не по правилам.",
}
INVALID_EMAIL = {
    "code": "invalid_email",
    "message": "Почта длиннее 254 символов.",
}
INVALID_REQUEST = {
    "code": "invalid_request",
    "message": "Нужны почта и пароль.",
}
INVALID_CREDENTIALS = {
    "code": "invalid_credentials",
    "message": "Почта или пароль не сходятся",
}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}


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


def _snapshot() -> tuple[frozenset[str], frozenset[str]]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            emails = connection.execute(select(accounts.c.email)).scalars().all()
            session_ids = connection.execute(select(sessions.c.id)).scalars().all()
    finally:
        engine.dispose()
    return frozenset(emails), frozenset(str(session_id) for session_id in session_ids)


def _account_rows() -> tuple[list[dict], list[dict]]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(accounts)).mappings().all()
            sessions_rows = connection.execute(select(sessions)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows], [dict(row) for row in sessions_rows]


def _cookie_value(response) -> str:
    header = response.headers["set-cookie"]
    parts = [part.strip().lower() for part in header.split(";")]
    assert "httponly" in parts
    assert "samesite=lax" in parts
    assert "path=/" in parts
    assert "secure" not in parts
    pair = header.split(";", 1)[0]
    name, value = pair.split("=", 1)
    assert name == "session"
    assert value == str(uuid.UUID(value))
    return value


def test_register_stores_the_normalized_email_and_session_cookie(database: None) -> None:
    raw_email = "  User Name@Example.COM  "
    stored_email = "user name@example.com"
    with TestClient(app) as client:
        response = _post(
            client,
            "/api/register",
            {"email": raw_email, "password": ACCEPTED},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["email_confirmed"] is False
    assert body["period"] is None
    assert body["quota"] is None
    assert body["account"]["email"] == stored_email
    assert body["account"]["name"] == ""
    assert ACCEPTED not in response.text
    cookie = _cookie_value(response)
    assert ACCEPTED not in cookie
    account_rows, session_rows = _account_rows()
    matched = [row for row in account_rows if row["email"] == stored_email]
    assert len(matched) == 1
    assert str(matched[0]["id"]) == body["account"]["id"]
    assert matched[0]["name"] == ""
    assert ACCEPTED not in matched[0]["password_hash"]
    account_sessions = [
        row for row in session_rows if row["account_id"] == matched[0]["id"]
    ]
    assert len(account_sessions) == 1
    assert str(account_sessions[0]["id"]) == cookie
    seen = account_sessions[0]["last_request_at"]
    assert seen.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - seen).total_seconds()) < 60


def test_same_email_in_another_spelling_is_taken(database: None) -> None:
    with TestClient(app) as client:
        first = _post(
            client,
            "/api/register",
            {"email": "  Second Person@Example.COM  ", "password": ACCEPTED},
        )
        assert first.status_code == 200
        before = _snapshot()
        second = _post(
            client,
            "/api/register",
            {"email": "SECOND person@example.com", "password": ACCEPTED},
        )
    assert second.status_code == 400
    assert second.json() == TAKEN
    assert "set-cookie" not in second.headers
    assert _snapshot() == before


def test_password_outside_the_rules_inserts_nothing(database: None) -> None:
    before = _snapshot()
    with TestClient(app) as client:
        response = _post(
            client,
            "/api/register",
            {"email": "rules@example.com", "password": "abcdefgh1"},
        )
    assert response.status_code == 400
    assert response.json() == INVALID_PASSWORD
    assert "set-cookie" not in response.headers
    assert _snapshot() == before


def test_login_with_either_spelling_opens_a_new_session(database: None) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            create_account(connection, "  Person@Example.COM  ", ACCEPTED)
    finally:
        engine.dispose()
    with TestClient(app) as client:
        normalized = _post(
            client,
            "/api/login",
            {"email": "person@example.com", "password": ACCEPTED},
        )
        raw = _post(
            client,
            "/api/login",
            {"email": "  PERSON@example.com  ", "password": ACCEPTED},
        )
    assert normalized.status_code == 200
    assert raw.status_code == 200
    assert normalized.json()["account"]["email"] == "person@example.com"
    assert raw.json()["account"]["email"] == "person@example.com"
    assert normalized.json()["account"]["name"] == ""
    assert normalized.json()["email_confirmed"] is False
    assert normalized.json()["period"] is None
    assert normalized.json()["quota"] is None
    assert raw.json()["email_confirmed"] is False
    first_cookie = _cookie_value(normalized)
    second_cookie = _cookie_value(raw)
    assert first_cookie != second_cookie
    account_id = uuid.UUID(normalized.json()["account"]["id"])
    _, session_rows = _account_rows()
    opened = [row for row in session_rows if row["account_id"] == account_id]
    assert {str(row["id"]) for row in opened} == {first_cookie, second_cookie}
    assert len(opened) == 2


def test_unknown_email_and_wrong_password_are_the_same_refusal(database: None) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            create_account(connection, "known@example.com", ACCEPTED)
    finally:
        engine.dispose()
    before = _snapshot()
    with TestClient(app) as client:
        unknown = _post(
            client,
            "/api/login",
            {"email": "missing@example.com", "password": ACCEPTED},
        )
        wrong = _post(
            client,
            "/api/login",
            {"email": "  Known@Example.com ", "password": OTHER},
        )
    assert unknown.status_code == wrong.status_code == 400
    assert unknown.json() == wrong.json() == INVALID_CREDENTIALS
    assert "set-cookie" not in unknown.headers
    assert "set-cookie" not in wrong.headers
    assert _snapshot() == before


def test_missing_or_foreign_origin_inserts_nothing(database: None) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            create_account(connection, "origin-login@example.com", ACCEPTED)
    finally:
        engine.dispose()
    before = _snapshot()
    cases = (
        ("/api/register", "origin-reg-missing@example.com", None),
        ("/api/register", "origin-reg-foreign@example.com", "http://evil.example"),
        ("/api/register", "origin-reg-path@example.com", "http://testserver/api"),
        ("/api/login", "origin-login@example.com", None),
        ("/api/login", "origin-login@example.com", "https://testserver"),
    )
    with TestClient(app) as client:
        for path, email, origin in cases:
            response = _post(
                client,
                path,
                {"email": email, "password": ACCEPTED},
                origin=origin,
            )
            assert response.status_code == 403
            assert response.json() == INVALID_ORIGIN
            assert "set-cookie" not in response.headers
    assert _snapshot() == before


def test_register_and_login_do_not_log_the_password(
    database: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            registered = _post(
                client,
                "/api/register",
                {"email": "quiet@example.com", "password": ACCEPTED},
            )
            logged_in = _post(
                client,
                "/api/login",
                {"email": "  Quiet@Example.com ", "password": ACCEPTED},
            )
    assert registered.status_code == 200
    assert logged_in.status_code == 200
    captured = caplog.text
    for record in caplog.records:
        captured += record.getMessage()
        captured += repr(record.args)
    assert ACCEPTED not in captured


def test_credentials_must_be_strings_and_email_has_the_stored_limit(
    database: None,
) -> None:
    before = _snapshot()
    with TestClient(app) as client:
        rejected = (
            _post(client, "/api/register", ["mail@example.com", ACCEPTED]),
            _post(client, "/api/register", {"email": "mail@example.com"}),
            _post(
                client,
                "/api/register",
                {"email": "mail@example.com", "password": 1},
            ),
            _post(client, "/api/login", {"password": ACCEPTED}),
            _post(client, "/api/login", {"email": None, "password": ACCEPTED}),
            client.post(
                "/api/register",
                content=b"",
                headers={"Origin": ORIGIN, "content-type": "application/json"},
            ),
        )
        too_long = _post(
            client,
            "/api/register",
            {"email": "  " + ("A" * 255) + "  ", "password": ACCEPTED},
        )
        empty = _post(
            client,
            "/api/register",
            {"email": "", "password": ACCEPTED},
        )
        max_length = _post(
            client,
            "/api/register",
            {"email": " " + ("B" * 254) + " ", "password": ACCEPTED},
        )
    for response in rejected:
        assert response.status_code == 400
        assert response.json() == INVALID_REQUEST
        assert "set-cookie" not in response.headers
    assert too_long.status_code == 400
    assert too_long.json() == INVALID_EMAIL
    assert empty.status_code == 200
    assert empty.json()["account"]["email"] == ""
    assert max_length.status_code == 200
    assert max_length.json()["account"]["email"] == "b" * 254
    emails, _session_ids = _snapshot()
    assert "a" * 255 not in emails
    assert "" in emails
    assert "b" * 254 in emails
    assert emails - before[0] == {"", "b" * 254}
