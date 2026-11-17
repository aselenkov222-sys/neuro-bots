import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from server.accounts import create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import accounts, desktops, sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
INVALID_DESKTOP = {
    "code": "invalid_request",
    "message": "Нужен идентификатор десктопа.",
}
NOT_FOUND = {"code": "not_found", "message": "Десктоп не найден."}
INVALID_TIMEZONE = {
    "code": "invalid_timezone",
    "message": "Неизвестная временная зона.",
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


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_confirmed(email: str) -> tuple[str, uuid.UUID]:
    confirmed_at = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(email_confirmed_at=confirmed_at)
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _account_desktops(account_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(desktops) if row["account_id"] == account_id]


def _session(cookie: str) -> dict:
    matched = [row for row in _rows(sessions) if row["id"] == uuid.UUID(cookie)]
    assert len(matched) == 1
    return matched[0]


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _post(client: TestClient, computer: uuid.UUID, origin: str = ORIGIN):
    return client.post(
        "/api/desktops",
        json={"id": str(computer)},
        headers={"Origin": origin},
    )


def _pulse(client: TestClient, computer: str, body: dict, origin: str = ORIGIN):
    return client.post(
        f"/api/desktops/{computer}/pulse",
        json=body,
        headers={"Origin": origin},
    )


def test_confirmed_session_that_never_posts_has_no_desktop(database: None) -> None:
    _cookie, account_id = _open_confirmed("no-desktop@example.com")
    with TestClient(app) as client:
        logged = client.post(
            "/api/login",
            json={"email": "no-desktop@example.com", "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert logged.status_code == 200
        me = client.get("/api/me")
        assert me.status_code == 200
    assert _account_desktops(account_id) == []
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"email": "register-no-desktop@example.com", "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert registered.status_code == 200
        registered_id = uuid.UUID(registered.json()["account"]["id"])
        me = client.get("/api/me")
        assert me.status_code == 200
    assert _account_desktops(registered_id) == []


def test_first_desktop_is_working_and_a_repeat_does_not_add_a_row(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("working-desktop@example.com")
    first = uuid.uuid4()
    second = uuid.uuid4()
    seen = _session(cookie)["last_request_at"]
    with _client(cookie) as client:
        posted = _post(client, first)
        assert posted.status_code == 200
        assert "set-cookie" not in posted.headers
        assert tuple(posted.json()) == ("id", "working")
        assert posted.json() == {"id": str(first), "working": True}
        other = _post(client, second)
        assert other.status_code == 200
        assert other.json() == {"id": str(second), "working": False}
        again = _post(client, first)
        assert again.status_code == 200
        assert again.json() == {"id": str(first), "working": True}
        repeated = _post(client, second)
        assert repeated.json() == {"id": str(second), "working": False}
    rows = _account_desktops(account_id)
    assert len(rows) == 2
    by_computer = {row["computer_id"]: row for row in rows}
    assert by_computer[first]["working"] is True
    assert by_computer[second]["working"] is False
    assert by_computer[first]["id"] != first
    assert by_computer[second]["id"] != second
    assert by_computer[first]["account_id"] == account_id
    assert _session(cookie)["last_request_at"] > seen


def test_logout_leaves_the_working_desktop_for_the_same_computer(
    database: None,
) -> None:
    email = "logout-desktop@example.com"
    _cookie, account_id = _open_confirmed(email)
    computer = uuid.uuid4()
    with TestClient(app) as client:
        logged = client.post(
            "/api/login",
            json={"email": email, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert logged.status_code == 200
        logged_cookie = client.cookies.get("session")
        assert isinstance(logged_cookie, str)
        posted = _post(client, computer)
        assert posted.json() == {"id": str(computer), "working": True}
        signed_out = client.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
        assert client.cookies.get("session") is None
    assert [row for row in _rows(sessions) if str(row["id"]) == logged_cookie] == []
    stored = _account_desktops(account_id)
    assert len(stored) == 1
    assert stored[0]["computer_id"] == computer
    assert stored[0]["working"] is True
    with TestClient(app) as client:
        logged = client.post(
            "/api/login",
            json={"email": email, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert logged.status_code == 200
        again = _post(client, computer)
        assert again.status_code == 200
        assert again.json() == {"id": str(computer), "working": True}
    stored = _account_desktops(account_id)
    assert len(stored) == 1
    assert stored[0]["working"] is True
    assert stored[0]["computer_id"] == computer


def test_another_account_keeps_its_own_working_desktop(database: None) -> None:
    first_cookie, first_account = _open_confirmed("desktop-a@example.com")
    second_cookie, second_account = _open_confirmed("desktop-b@example.com")
    shared = uuid.uuid4()
    other = uuid.uuid4()
    with _client(first_cookie) as client:
        posted = _post(client, shared)
        assert posted.json() == {"id": str(shared), "working": True}
    with _client(second_cookie) as client:
        posted = _post(client, other)
        assert posted.json() == {"id": str(other), "working": True}
    assert {
        row["computer_id"]: row["working"] for row in _account_desktops(first_account)
    } == {shared: True}
    assert {
        row["computer_id"]: row["working"] for row in _account_desktops(second_account)
    } == {other: True}


def test_the_same_computer_can_be_working_for_two_accounts(database: None) -> None:
    first_cookie, first_account = _open_confirmed("shared-a@example.com")
    second_cookie, second_account = _open_confirmed("shared-b@example.com")
    shared = uuid.uuid4()
    with _client(first_cookie) as client:
        posted = _post(client, shared)
        assert posted.json() == {"id": str(shared), "working": True}
    with _client(second_cookie) as client:
        posted = _post(client, shared)
        assert posted.status_code == 200
        assert posted.json() == {"id": str(shared), "working": True}
    first_rows = _account_desktops(first_account)
    second_rows = _account_desktops(second_account)
    assert len(first_rows) == 1
    assert len(second_rows) == 1
    assert first_rows[0]["id"] != second_rows[0]["id"]
    assert first_rows[0]["computer_id"] == shared
    assert second_rows[0]["computer_id"] == shared
    assert first_rows[0]["working"] is True
    assert second_rows[0]["working"] is True


def test_desktop_post_rejects_a_missing_session_foreign_origin_and_bad_id(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("desktop-errors@example.com")
    computer = uuid.uuid4()
    with TestClient(app) as client:
        missing = client.post(
            "/api/desktops",
            json={"id": str(computer)},
            headers={"Origin": ORIGIN},
        )
    assert missing.status_code == 401
    assert missing.json() == UNAUTHENTICATED
    with _client(cookie) as client:
        foreign = client.post(
            "/api/desktops",
            json={"id": str(computer)},
            headers={"Origin": "https://evil.example"},
        )
        assert foreign.status_code == 403
        assert foreign.json() == INVALID_ORIGIN
        seen = _session(cookie)["last_request_at"]
        bad_bodies = (
            {"id": "not-a-uuid"},
            {"id": ""},
            {"id": 1},
            {"id": None},
            {},
            {"computer": str(computer)},
        )
        for body in bad_bodies:
            bad = client.post(
                "/api/desktops",
                json=body,
                headers={"Origin": ORIGIN},
            )
            assert bad.status_code == 400
            assert bad.json() == INVALID_DESKTOP
        for content in (b"[]", b"nope", b""):
            bad = client.post(
                "/api/desktops",
                content=content,
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            )
            assert bad.status_code == 400
            assert bad.json() == INVALID_DESKTOP
        assert _session(cookie)["last_request_at"] == seen
    assert _account_desktops(account_id) == []


def test_unconfirmed_session_can_claim_the_working_desktop(database: None) -> None:
    computer = uuid.uuid4()
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"email": "unconfirmed-desktop@example.com", "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert registered.status_code == 200
        assert registered.json()["email_confirmed"] is False
        account_id = uuid.UUID(registered.json()["account"]["id"])
        assert _account_desktops(account_id) == []
        posted = _post(client, computer)
    assert posted.status_code == 200
    assert posted.json() == {"id": str(computer), "working": True}
    rows = _account_desktops(account_id)
    assert len(rows) == 1
    assert rows[0]["working"] is True
    assert rows[0]["computer_id"] == computer


def test_losing_first_claim_is_stored_as_not_working(database: None) -> None:
    cookie, account_id = _open_confirmed("race-desktop@example.com")
    winner = uuid.uuid4()
    loser = uuid.uuid4()
    engine = create_db_engine()
    started = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def hold() -> None:
        try:
            with engine.connect() as connection:
                with connection.begin():
                    connection.execute(
                        desktops.insert().values(
                            id=uuid.uuid4(),
                            account_id=account_id,
                            computer_id=winner,
                            working=True,
                            created_at=datetime.now(timezone.utc),
                        )
                    )
                    started.set()
                    assert release.wait(10)
        except BaseException as error:
            holder_errors.append(error)
            started.set()
            release.set()

    holder = threading.Thread(target=hold)
    holder.start()
    result: dict[str, object] = {}
    try:
        assert started.wait(10)
        assert holder_errors == []

        def post() -> None:
            try:
                with _client(cookie) as client:
                    result["response"] = _post(client, loser)
            except BaseException as error:
                result["error"] = error

        poster = threading.Thread(target=post)
        poster.start()
        waiting = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                waiting = bool(
                    connection.execute(
                        text(
                            """
                            SELECT count(*)
                            FROM pg_stat_activity
                            WHERE datname = current_database()
                              AND wait_event_type = 'Lock'
                            """
                        )
                    ).scalar_one()
                )
            if waiting:
                break
            time.sleep(0.05)
        release.set()
        poster.join(10)
        assert waiting
        response = result["response"]
        assert getattr(response, "status_code", None) == 200
        assert response.json() == {"id": str(loser), "working": False}
    finally:
        release.set()
        holder.join(10)
        engine.dispose()
    assert holder_errors == []
    rows = {row["computer_id"]: row for row in _account_desktops(account_id)}
    assert set(rows) == {winner, loser}
    assert rows[winner]["working"] is True
    assert rows[loser]["working"] is False


def test_working_desktop_pulse_stores_timezone_and_a_later_pulse_replaces_it(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("pulse-desktop@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        assert _post(client, computer).json() == {"id": str(computer), "working": True}
        seen = _session(cookie)["last_request_at"]
        pulsed = _pulse(client, str(computer), {"timezone": "Europe/Moscow"})
        assert pulsed.status_code == 200
        assert "set-cookie" not in pulsed.headers
        assert tuple(pulsed.json()) == ("timezone",)
        assert pulsed.json() == {"timezone": "Europe/Moscow"}
        stored = _account_desktops(account_id)
        assert len(stored) == 1
        assert stored[0]["timezone"] == "Europe/Moscow"
        assert stored[0]["last_pulse_at"] is not None
        assert stored[0]["last_pulse_at"].tzinfo is not None
        assert _session(cookie)["last_request_at"] > seen
        first_at = stored[0]["last_pulse_at"]
        again = _pulse(client, str(computer), {"timezone": "Asia/Tokyo"})
        assert again.status_code == 200
        assert again.json() == {"timezone": "Asia/Tokyo"}
    replaced = _account_desktops(account_id)[0]
    assert replaced["timezone"] == "Asia/Tokyo"
    assert replaced["last_pulse_at"] > first_at
    assert replaced["working"] is True


def test_non_working_desktop_pulse_is_not_stored(database: None) -> None:
    cookie, account_id = _open_confirmed("pulse-idle@example.com")
    working = uuid.uuid4()
    idle = uuid.uuid4()
    with _client(cookie) as client:
        assert _post(client, working).json()["working"] is True
        assert _post(client, idle).json()["working"] is False
        kept = _pulse(client, str(working), {"timezone": "Europe/Moscow"})
        assert kept.status_code == 200
        before = {row["computer_id"]: row for row in _account_desktops(account_id)}
        missed = _pulse(client, str(idle), {"timezone": "Asia/Tokyo"})
        assert missed.status_code == 404
        assert missed.json() == NOT_FOUND
    after = {row["computer_id"]: row for row in _account_desktops(account_id)}
    assert after[working]["timezone"] == "Europe/Moscow"
    assert after[working]["last_pulse_at"] == before[working]["last_pulse_at"]
    assert after[idle]["timezone"] is None
    assert after[idle]["last_pulse_at"] is None
    assert after[idle]["working"] is False


def test_pulse_rejects_unknown_timezone_missing_session_foreign_origin_and_bad_id(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("pulse-errors@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        assert _post(client, computer).json()["working"] is True
        assert _pulse(client, str(computer), {"timezone": "Europe/Moscow"}).status_code == 200
        before = _account_desktops(account_id)[0]
        seen = _session(cookie)["last_request_at"]
        for body in (
            {"timezone": "Not/AZone"},
            {"timezone": ""},
            {},
            {"timezone": 1},
            {"timezone": None},
            {"zone": "Europe/Moscow"},
        ):
            bad = _pulse(client, str(computer), body)
            assert bad.status_code == 400
            assert bad.json() == INVALID_TIMEZONE
        for content in (b"[]", b"nope", b""):
            bad = client.post(
                f"/api/desktops/{computer}/pulse",
                content=content,
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            )
            assert bad.status_code == 400
            assert bad.json() == INVALID_TIMEZONE
        assert _session(cookie)["last_request_at"] == seen
        foreign = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Asia/Tokyo"},
            headers={"Origin": "https://evil.example"},
        )
        assert foreign.status_code == 403
        assert foreign.json() == INVALID_ORIGIN
        bad_id = client.post(
            "/api/desktops/not-a-uuid/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert bad_id.status_code == 404
        assert bad_id.json() == NOT_FOUND
    with TestClient(app) as client:
        missing = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
    assert missing.status_code == 401
    assert missing.json() == UNAUTHENTICATED
    row = _account_desktops(account_id)[0]
    assert row["timezone"] == before["timezone"]
    assert row["last_pulse_at"] == before["last_pulse_at"]


def test_unconfirmed_session_can_pulse_its_working_desktop(database: None) -> None:
    computer = uuid.uuid4()
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"email": "pulse-unconfirmed@example.com", "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
        assert registered.status_code == 200
        assert registered.json()["email_confirmed"] is False
        account_id = uuid.UUID(registered.json()["account"]["id"])
        assert _post(client, computer).json()["working"] is True
        pulsed = _pulse(client, str(computer), {"timezone": "Europe/Moscow"})
    assert pulsed.status_code == 200
    assert pulsed.json() == {"timezone": "Europe/Moscow"}
    stored = _account_desktops(account_id)
    assert len(stored) == 1
    assert stored[0]["timezone"] == "Europe/Moscow"
    assert stored[0]["last_pulse_at"] is not None


def test_pulse_does_not_write_another_accounts_desktop(database: None) -> None:
    first_cookie, first_account = _open_confirmed("pulse-shared-a@example.com")
    second_cookie, second_account = _open_confirmed("pulse-shared-b@example.com")
    shared = uuid.uuid4()
    with _client(first_cookie) as client:
        assert _post(client, shared).json()["working"] is True
    with _client(second_cookie) as client:
        assert _post(client, shared).json()["working"] is True
        pulsed = _pulse(client, str(shared), {"timezone": "Europe/Moscow"})
        assert pulsed.status_code == 200
    assert _account_desktops(second_account)[0]["timezone"] == "Europe/Moscow"
    untouched = _account_desktops(first_account)[0]
    assert untouched["computer_id"] == shared
    assert untouched["working"] is True
    assert untouched["timezone"] is None
    assert untouched["last_pulse_at"] is None
