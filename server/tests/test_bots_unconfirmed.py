import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from server.accounts import create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import accounts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
EMAIL_UNCONFIRMED = {
    "code": "email_unconfirmed",
    "message": "Почта ещё не подтверждена.",
}
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
ROUTES = (
    ("GET", "/api/bots"),
    ("POST", "/api/bots"),
    ("GET", "/api/bots/not-a-uuid"),
    ("PATCH", "/api/bots/not-a-uuid"),
    ("GET", "/api/bots/not-a-uuid/messages"),
    ("POST", "/api/bots/not-a-uuid/messages"),
)
MUTATING = {"POST", "PATCH"}


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


def _open_session(email: str) -> str:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            return str(create_session(connection, account_id))
    finally:
        engine.dispose()


def _confirm(email: str) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                accounts.update()
                .where(accounts.c.email == email)
                .values(
                    email_confirmed_at=datetime(
                        2026, 11, 16, tzinfo=timezone.utc
                    )
                )
            )
    finally:
        engine.dispose()


def _state() -> tuple[tuple[str, ...], dict[str, list[tuple[object, ...]]]]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            tables = tuple(
                sorted(
                    connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables"
                            " WHERE schemaname = 'public'"
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            stored: dict[str, list[tuple[object, ...]]] = {}
            for table in (
                "accounts",
                "sessions",
                "confirmation_links",
                "bots",
                "chats",
                "messages",
                "subscription_periods",
                "quota_counters",
                "quota_charges",
            ):
                result = connection.execute(text(f"SELECT * FROM {table}"))
                stored[table] = [tuple(row) for row in result]
    finally:
        engine.dispose()
    return tables, stored


def _call(
    client: TestClient, method: str, path: str, *, origin: str | None
) -> object:
    headers = {} if origin is None else {"Origin": origin}
    return client.request(method, path, headers=headers)


def test_unconfirmed_session_is_refused_and_writes_nothing(database: None) -> None:
    cookie = _open_session("unconfirmed-bots@example.com")
    before = _state()
    with TestClient(app) as client:
        client.cookies.set("session", cookie)
        responses = [
            _call(
                client,
                method,
                path,
                origin=ORIGIN if method in MUTATING else None,
            )
            for method, path in ROUTES
        ]
        foreign_list = _call(
            client, "GET", "/api/bots", origin="http://evil.example"
        )
    assert [response.status_code for response in responses] == [403] * len(ROUTES)
    assert [response.json() for response in responses] == [EMAIL_UNCONFIRMED] * len(
        ROUTES
    )
    assert all("set-cookie" not in response.headers for response in responses)
    assert foreign_list.status_code == 403
    assert foreign_list.json() == EMAIL_UNCONFIRMED
    assert _state() == before


def test_post_and_patch_reject_a_foreign_origin_before_confirmation(
    database: None,
) -> None:
    cookie = _open_session("origin-bots@example.com")
    before = _state()
    paths = ("/api/bots", "/api/bots/not-a-uuid")
    methods = ("POST", "PATCH")
    with TestClient(app) as client:
        client.cookies.set("session", cookie)
        missing = [
            _call(client, method, path, origin=None)
            for method, path in zip(methods, paths, strict=True)
        ]
        foreign = [
            _call(client, method, path, origin="http://evil.example")
            for method, path in zip(methods, paths, strict=True)
        ]
    assert [response.status_code for response in missing + foreign] == [403] * 4
    assert [response.json() for response in missing + foreign] == [INVALID_ORIGIN] * 4
    assert _state() == before


def test_missing_session_is_unauthenticated(database: None) -> None:
    before = _state()
    with TestClient(app) as client:
        responses = [
            _call(
                client,
                method,
                path,
                origin=ORIGIN if method in MUTATING else None,
            )
            for method, path in ROUTES
        ]
    assert _state() == before
    expired = _open_session("expired-bots@example.com")
    opened = _state()
    past = datetime.now(timezone.utc) - timedelta(days=31)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sessions SET last_request_at = :past"
                    " WHERE id = :session_id"
                ),
                {"past": past, "session_id": uuid.UUID(expired)},
            )
    finally:
        engine.dispose()
    after_expiry = _state()
    with TestClient(app) as client:
        client.cookies.set("session", expired)
        stale = client.get("/api/bots")
        client.cookies.set("session", "not-a-session")
        malformed = client.get("/api/bots")
    assert [response.status_code for response in responses] == [401] * len(ROUTES)
    assert [response.json() for response in responses] == [UNAUTHENTICATED] * len(
        ROUTES
    )
    assert stale.status_code == 401
    assert stale.json() == UNAUTHENTICATED
    assert malformed.status_code == 401
    assert malformed.json() == UNAUTHENTICATED
    assert opened != before
    assert _state() == after_expiry


def _assert_only_last_request_moved(
    before: tuple[tuple[str, ...], dict[str, list[tuple[object, ...]]]],
    after: tuple[tuple[str, ...], dict[str, list[tuple[object, ...]]]],
    cookie: str,
) -> None:
    assert before[0] == after[0]
    for table, rows in before[1].items():
        if table != "sessions":
            assert after[1][table] == rows
    session_id = uuid.UUID(cookie)
    before_sessions = {row[0]: row for row in before[1]["sessions"]}
    after_sessions = {row[0]: row for row in after[1]["sessions"]}
    assert set(before_sessions) == set(after_sessions)
    moved = False
    for sid, row in before_sessions.items():
        if sid == session_id:
            assert after_sessions[sid][1] == row[1]
            assert after_sessions[sid][2] > row[2]
            moved = True
        else:
            assert after_sessions[sid] == row
    assert moved


def test_confirmed_session_is_not_email_unconfirmed(database: None) -> None:
    email = "confirmed-bots@example.com"
    cookie = _open_session(email)
    _confirm(email)
    before = _state()
    with TestClient(app) as client:
        client.cookies.set("session", cookie)
        listed = _call(client, "GET", "/api/bots", origin=None)
        after_list = _state()
        posted = _call(client, "POST", "/api/bots", origin=ORIGIN)
        after_post = _state()
        rest = {
            (method, path): _call(
                client,
                method,
                path,
                origin=ORIGIN if method in MUTATING else None,
            )
            for method, path in ROUTES
            if path != "/api/bots"
        }
    assert listed.status_code == 200
    assert listed.json() == {"bots": []}
    assert listed.json() != EMAIL_UNCONFIRMED
    assert "email_unconfirmed" not in listed.text
    assert "set-cookie" not in listed.headers
    _assert_only_last_request_moved(before, after_list, cookie)
    assert posted.status_code == 400
    assert posted.json() == {
        "code": "invalid_request",
        "message": "Нужен JSON-объект.",
    }
    assert posted.json() != EMAIL_UNCONFIRMED
    assert "email_unconfirmed" not in posted.text
    assert "set-cookie" not in posted.headers
    assert after_post == after_list
    not_found = {"code": "not_found", "message": "Бот не найден."}
    expected = {
        ("GET", "/api/bots/not-a-uuid"): (404, not_found),
        ("PATCH", "/api/bots/not-a-uuid"): (404, not_found),
        ("GET", "/api/bots/not-a-uuid/messages"): (404, not_found),
        ("POST", "/api/bots/not-a-uuid/messages"): (404, not_found),
    }
    assert set(rest) == set(expected)
    for route, response in rest.items():
        status, body = expected[route]
        assert response.status_code == status
        assert response.json() == body
        assert response.json() != EMAIL_UNCONFIRMED
        assert "email_unconfirmed" not in response.text
        assert "set-cookie" not in response.headers
    assert _state() == after_post
