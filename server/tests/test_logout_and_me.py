import logging
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
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


def _login(client: TestClient, email: str):
    return client.post(
        "/api/login",
        json={"email": email, "password": ACCEPTED},
        headers={"Origin": ORIGIN},
    )


def _client(cookie: str | None = None) -> TestClient:
    client = TestClient(app)
    if cookie is not None:
        client.cookies.set("session", cookie)
    return client


def _cookie_value(response) -> str:
    header = response.headers["set-cookie"]
    pair = header.split(";", 1)[0]
    name, value = pair.split("=", 1)
    assert name == "session"
    assert value == str(uuid.UUID(value))
    return value


def _session(session_id: uuid.UUID) -> dict | None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(select(sessions).where(sessions.c.id == session_id))
                .mappings()
                .one_or_none()
            )
    finally:
        engine.dispose()
    if row is None:
        return None
    return dict(row)


def _set_last_request(session_id: uuid.UUID, when: datetime) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                sessions.update()
                .where(sessions.c.id == session_id)
                .values(last_request_at=when)
            )
    finally:
        engine.dispose()


def _open_account(email: str) -> uuid.UUID:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            return create_account(connection, email, ACCEPTED)
    finally:
        engine.dispose()


def _account_body(account_id: uuid.UUID, email: str) -> dict:
    return {
        "account": {"id": str(account_id), "email": email, "name": ""},
        "email_confirmed": False,
        "period": None,
        "quota": None,
    }


def test_logout_deletes_only_the_current_session(
    database: None, caplog: pytest.LogCaptureFixture
) -> None:
    email = "two-sessions@example.com"
    account_id = _open_account(email)
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            first = _login(client, email)
            second = _login(client, email)
            assert first.status_code == 200
            assert second.status_code == 200
            first_cookie = _cookie_value(first)
            second_cookie = _cookie_value(second)
            assert first_cookie != second_cookie
        with _client(first_cookie) as first_session:
            logged_out = first_session.post("/api/logout", headers={"Origin": ORIGIN})
        with _client(second_cookie) as second_session:
            still_in = second_session.get("/api/me")
    assert logged_out.status_code == 200
    assert logged_out.json() == {}
    header = logged_out.headers["set-cookie"]
    parts = [part.strip().lower() for part in header.split(";")]
    assert "httponly" in parts
    assert "samesite=lax" in parts
    assert "path=/" in parts
    assert "max-age=0" in parts
    assert "secure" not in parts
    assert header.split(";", 1)[0].split("=", 1)[0] == "session"
    assert _session(uuid.UUID(first_cookie)) is None
    remaining = _session(uuid.UUID(second_cookie))
    assert remaining is not None
    assert remaining["account_id"] == account_id
    assert still_in.status_code == 200
    assert still_in.json() == _account_body(account_id, email)
    captured = caplog.text
    for record in caplog.records:
        captured += record.getMessage()
        captured += repr(record.args)
    assert ACCEPTED not in captured
    assert first_cookie not in captured
    assert second_cookie not in captured


def test_logout_without_our_origin_keeps_the_session(database: None) -> None:
    email = "origin-logout@example.com"
    _open_account(email)
    with TestClient(app) as client:
        logged_in = _login(client, email)
        cookie = _cookie_value(logged_in)
    session_id = uuid.UUID(cookie)
    before = _session(session_id)["last_request_at"]
    with _client(cookie) as session:
        missing = session.post("/api/logout")
    with _client(cookie) as session:
        foreign = session.post(
            "/api/logout",
            headers={"Origin": "http://evil.example"},
        )
    assert missing.status_code == 403
    assert foreign.status_code == 403
    assert missing.json() == foreign.json() == INVALID_ORIGIN
    kept = _session(session_id)
    assert kept is not None
    assert kept["last_request_at"] == before


def test_me_without_a_cookie_is_unauthenticated(database: None) -> None:
    email = "kept@example.com"
    _open_account(email)
    with TestClient(app) as client:
        logged_in = _login(client, email)
        cookie = _cookie_value(logged_in)
    session_id = uuid.UUID(cookie)
    before = _session(session_id)["last_request_at"]
    with _client() as anonymous:
        missing = anonymous.get("/api/me")
    with _client(str(uuid.uuid4())) as unknown_session:
        unknown = unknown_session.get("/api/me")
    with _client("not-a-uuid") as malformed:
        not_uuid = malformed.get("/api/me")
        logged_out = malformed.post("/api/logout", headers={"Origin": ORIGIN})
    assert missing.status_code == unknown.status_code == not_uuid.status_code == 401
    assert logged_out.status_code == 401
    assert missing.json() == unknown.json() == not_uuid.json() == UNAUTHENTICATED
    assert logged_out.json() == UNAUTHENTICATED
    kept = _session(session_id)
    assert kept is not None
    assert kept["last_request_at"] == before


def test_session_older_than_30_days_is_rejected_and_kept(database: None) -> None:
    email = "expired@example.com"
    _open_account(email)
    with TestClient(app) as client:
        logged_in = _login(client, email)
        cookie = _cookie_value(logged_in)
    session_id = uuid.UUID(cookie)
    _set_last_request(
        session_id,
        datetime.now(timezone.utc) - timedelta(days=30, minutes=1),
    )
    before = _session(session_id)["last_request_at"]
    with _client(cookie) as session:
        me = session.get("/api/me")
        logged_out = session.post("/api/logout", headers={"Origin": ORIGIN})
    assert me.status_code == 401
    assert logged_out.status_code == 401
    assert me.json() == logged_out.json() == UNAUTHENTICATED
    kept = _session(session_id)
    assert kept is not None
    assert kept["last_request_at"] == before


def test_session_at_exactly_30_days_is_not_valid(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("server.auth.utc_now", lambda: frozen)
    email = "boundary@example.com"
    account_id = _open_account(email)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            session_id = create_session(connection, account_id)
            connection.execute(
                sessions.update()
                .where(sessions.c.id == session_id)
                .values(last_request_at=frozen - timedelta(days=30))
            )
    finally:
        engine.dispose()
    cookie = str(session_id)
    before = _session(session_id)["last_request_at"]
    with _client(cookie) as session:
        me = session.get("/api/me")
        logged_out = session.post("/api/logout", headers={"Origin": ORIGIN})
    assert me.status_code == 401
    assert logged_out.status_code == 401
    kept = _session(session_id)
    assert kept is not None
    assert kept["last_request_at"] == before


def test_successful_me_restarts_the_30_days(database: None) -> None:
    email = "fresh@example.com"
    account_id = _open_account(email)
    with TestClient(app) as client:
        logged_in = _login(client, email)
        cookie = _cookie_value(logged_in)
        session_id = uuid.UUID(cookie)
        _set_last_request(session_id, datetime.now(timezone.utc) - timedelta(days=29))
        before = _session(session_id)["last_request_at"]
        me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json() == _account_body(account_id, email)
    after = _session(session_id)["last_request_at"]
    assert after > before
    assert after.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - after).total_seconds()) < 60
