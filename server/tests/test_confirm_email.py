import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from server.accounts import calendar_month_end, format_utc
from server.confirmation import hash_token
from server.db import create_db_engine
from server.models import accounts, confirmation_links, sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ACCEPTED = "abcdef1!"
CONFIRM_PATH = "/api/confirm-email?token="
EXPIRED = "Ссылка больше не действует"
REFUSAL = {
    "email_confirmed": False,
    "destination": "login",
    "message": EXPIRED,
}
TARIFF_REFUSAL = {
    "email_confirmed": False,
    "destination": "tariff",
    "message": EXPIRED,
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


class Journal:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        assert process.stdout is not None
        self._thread = threading.Thread(
            target=self._read,
            args=(process.stdout,),
            daemon=True,
        )
        self._thread.start()

    def _read(self, stream) -> None:
        for line in stream:
            with self._lock:
                self._chunks.append(line)

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def join(self) -> None:
        self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(process: subprocess.Popen[str], journal: Journal) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    journal.join()


def _wait_until(journal: Journal, predicate, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = journal.text()
        if predicate(current):
            return current
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    current = journal.text()
    assert predicate(current), current
    return current


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.fixture(scope="module")
def running_server(database: None):
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    journal = Journal(process)
    try:
        with httpx.Client(timeout=5) as client:
            _wait_until(
                journal,
                lambda text: (
                    "Application startup complete." in text and _health_ok(client, origin)
                ),
                timeout=20,
            )
        yield origin, journal
    finally:
        _stop(process, journal)


def _post(client: httpx.Client, origin: str, path: str, payload: object):
    return client.post(
        f"{origin}{path}",
        json=payload,
        headers={"Origin": origin},
    )


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _account(account_id: uuid.UUID) -> dict:
    matched = [row for row in _rows(accounts) if row["id"] == account_id]
    assert len(matched) == 1
    return matched[0]


def _account_by_email(email: str) -> dict:
    matched = [row for row in _rows(accounts) if row["email"] == email]
    assert len(matched) == 1
    return matched[0]


def _link(account_id: uuid.UUID) -> dict:
    matched = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(matched) == 1
    return matched[0]


def _confirmation_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if CONFIRM_PATH in line and line.startswith("http")]


def _register(origin: str, journal: Journal, client: httpx.Client, email: str) -> str:
    before = journal.text()
    response = _post(
        client,
        origin,
        "/api/register",
        {"email": email, "password": ACCEPTED},
    )
    assert response.status_code == 200
    assert response.json()["email_confirmed"] is False
    assert response.json()["period"] is None
    output = _wait_until(
        journal,
        lambda text: len(_confirmation_lines(text[len(before) :])) >= 1,
    )
    lines = _confirmation_lines(output[len(before) :])
    assert len(lines) == 1
    matched = re.fullmatch(
        rf"{re.escape(origin)}{re.escape(CONFIRM_PATH)}([^ ]+) {re.escape(email)}",
        lines[0],
    )
    assert matched is not None
    return matched.group(1)


def _confirm(
    journal: Journal,
    client: httpx.Client,
    origin: str,
    token: str | None,
    origin_header: str | None = None,
    send_origin: bool = False,
):
    before = journal.text()
    headers = {}
    if send_origin:
        headers["Origin"] = origin_header if origin_header is not None else origin
    params = {} if token is None else {"token": token}
    response = client.get(
        f"{origin}/api/confirm-email",
        params=params,
        headers=headers,
    )
    _wait_until(
        journal,
        lambda text: text[len(before) :].count("GET /api/confirm-email") >= 1,
    )
    new = journal.text()[len(before) :]
    assert not any(line.startswith(f"{origin}{CONFIRM_PATH}") for line in new.splitlines())
    assert ACCEPTED not in new
    assert "set-cookie" not in response.headers
    return response


def _assert_active_period(body: dict, confirmed_at: datetime) -> None:
    assert body["email_confirmed"] is True
    assert body["quota"] is None
    assert body["period"] == {
        "started_at": format_utc(confirmed_at),
        "ends_at": format_utc(calendar_month_end(confirmed_at)),
        "active": True,
    }


def test_calendar_month_keeps_the_utc_time_of_day() -> None:
    cases = (
        (
            datetime(2026, 11, 16, tzinfo=timezone.utc),
            datetime(2026, 12, 16, tzinfo=timezone.utc),
            "2026-11-16T00:00:00+00:00",
            "2026-12-16T00:00:00+00:00",
        ),
        (
            datetime(2026, 1, 31, 15, tzinfo=timezone.utc),
            datetime(2026, 2, 28, 15, tzinfo=timezone.utc),
            "2026-01-31T15:00:00+00:00",
            "2026-02-28T15:00:00+00:00",
        ),
        (
            datetime(2024, 1, 31, 15, tzinfo=timezone.utc),
            datetime(2024, 2, 29, 15, tzinfo=timezone.utc),
            "2024-01-31T15:00:00+00:00",
            "2024-02-29T15:00:00+00:00",
        ),
        (
            datetime(2026, 3, 31, 8, tzinfo=timezone.utc),
            datetime(2026, 4, 30, 8, tzinfo=timezone.utc),
            "2026-03-31T08:00:00+00:00",
            "2026-04-30T08:00:00+00:00",
        ),
    )
    for start, end, start_text, end_text in cases:
        assert calendar_month_end(start) == end
        assert format_utc(start) == start_text
        assert format_utc(end) == end_text


def test_valid_token_with_the_registration_session_opens_bots(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "session-confirm@example.com"
    with httpx.Client(timeout=5) as client:
        token = _register(origin, journal, client, email)
        opened = _confirm(journal, client, origin, token)
        assert opened.status_code == 200
        assert opened.json() == {"email_confirmed": True, "destination": "bots"}
        assert token not in opened.text
        me = client.get(f"{origin}/api/me")
    assert me.status_code == 200
    account_id = uuid.UUID(me.json()["account"]["id"])
    account = _account(account_id)
    link = _link(account_id)
    confirmed_at = account["email_confirmed_at"]
    assert confirmed_at is not None
    assert confirmed_at == link["revoked_at"]
    assert confirmed_at.tzinfo is not None
    assert confirmed_at.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - confirmed_at).total_seconds()) < 10
    assert link["token_hash"] == hash_token(token)
    assert token not in str(link["token_hash"])
    _assert_active_period(me.json(), confirmed_at)


def test_valid_token_without_a_session_confirms_and_login_shows_the_period(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "no-session-confirm@example.com"
    with httpx.Client(timeout=5) as client:
        token = _register(origin, journal, client, email)
    with httpx.Client(timeout=5) as client:
        opened = _confirm(journal, client, origin, token)
        assert opened.status_code == 200
        assert opened.json() == {"email_confirmed": True, "destination": "login"}
    account_id = _account_by_email(email)["id"]
    confirmed_at = _account(account_id)["email_confirmed_at"]
    assert confirmed_at == _link(account_id)["revoked_at"]
    with httpx.Client(timeout=5) as client:
        logged_in = _post(
            client,
            origin,
            "/api/login",
            {"email": email, "password": ACCEPTED},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["email_confirmed"] is True
        me = client.get(f"{origin}/api/me")
    assert me.status_code == 200
    _assert_active_period(me.json(), confirmed_at)


def test_another_accounts_session_does_not_open_bots(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    with httpx.Client(timeout=5) as owner:
        token = _register(origin, journal, owner, "owner-confirm@example.com")
    with httpx.Client(timeout=5) as other:
        _register(origin, journal, other, "other-confirm@example.com")
        opened = _confirm(journal, other, origin, token)
        assert opened.status_code == 200
        assert opened.json() == {"email_confirmed": True, "destination": "login"}
        me = other.get(f"{origin}/api/me")
    assert me.status_code == 200
    assert me.json()["email_confirmed"] is False
    assert me.json()["period"] is None
    owner_id = uuid.UUID(me.json()["account"]["id"])
    # me above is the other account; find the owner by email
    owner_row = next(row for row in _rows(accounts) if row["email"] == "owner-confirm@example.com")
    other_row = _account(owner_id)
    assert other_row["email"] == "other-confirm@example.com"
    assert other_row["email_confirmed_at"] is None
    assert owner_row["email_confirmed_at"] is not None
    assert _link(owner_row["id"])["revoked_at"] == owner_row["email_confirmed_at"]


def test_second_open_does_not_move_the_confirmation_instant(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "repeat-confirm@example.com"
    with httpx.Client(timeout=5) as client:
        token = _register(origin, journal, client, email)
        first = _confirm(journal, client, origin, token)
        assert first.json()["destination"] == "bots"
        account_id = uuid.UUID(client.get(f"{origin}/api/me").json()["account"]["id"])
        confirmed_at = _account(account_id)["email_confirmed_at"]
        revoked_at = _link(account_id)["revoked_at"]
        second = _confirm(journal, client, origin, token)
    assert second.status_code == 200
    assert second.json() == TARIFF_REFUSAL
    assert second.json()["message"] == EXPIRED
    assert _account(account_id)["email_confirmed_at"] == confirmed_at
    assert _link(account_id)["revoked_at"] == revoked_at
    with httpx.Client(timeout=5) as client:
        logged_in = _post(
            client,
            origin,
            "/api/login",
            {"email": email, "password": ACCEPTED},
        )
        assert logged_in.status_code == 200
        me = client.get(f"{origin}/api/me")
    _assert_active_period(me.json(), confirmed_at)


def test_expired_token_does_not_confirm(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "expired-confirm@example.com"
    with httpx.Client(timeout=5) as client:
        token = _register(origin, journal, client, email)
        account_id = _account_by_email(email)["id"]
        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    confirmation_links.update()
                    .where(confirmation_links.c.token_hash == hash_token(token))
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
                )
        finally:
            engine.dispose()
        opened = _confirm(journal, client, origin, token)
    assert opened.status_code == 200
    assert opened.json() == TARIFF_REFUSAL
    assert _account(account_id)["email_confirmed_at"] is None
    assert _link(account_id)["revoked_at"] is None
    assert _link(account_id)["expires_at"] < datetime.now(timezone.utc)


def test_foreign_and_missing_origin_still_confirm(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    with httpx.Client(timeout=5) as client:
        foreign_token = _register(origin, journal, client, "foreign-origin@example.com")
        missing_token = _register(origin, journal, client, "missing-origin@example.com")
    with httpx.Client(timeout=5) as client:
        foreign = _confirm(
            journal,
            client,
            origin,
            foreign_token,
            origin_header="http://evil.example",
            send_origin=True,
        )
        missing = _confirm(journal, client, origin, missing_token)
    assert foreign.status_code == missing.status_code == 200
    assert foreign.json() == {"email_confirmed": True, "destination": "login"}
    assert missing.json() == {"email_confirmed": True, "destination": "login"}
    foreign_row = next(row for row in _rows(accounts) if row["email"] == "foreign-origin@example.com")
    missing_row = next(row for row in _rows(accounts) if row["email"] == "missing-origin@example.com")
    assert foreign_row["email_confirmed_at"] is not None
    assert missing_row["email_confirmed_at"] is not None
    assert _link(foreign_row["id"])["revoked_at"] == foreign_row["email_confirmed_at"]
    assert _link(missing_row["id"])["revoked_at"] == missing_row["email_confirmed_at"]


def test_missing_empty_and_unknown_tokens_do_not_confirm(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    with httpx.Client(timeout=5) as client:
        _register(origin, journal, client, "invalid-token@example.com")
        with_session = (
            _confirm(journal, client, origin, None),
            _confirm(journal, client, origin, ""),
            _confirm(journal, client, origin, "not-a-real-token"),
        )
    for response in with_session:
        assert response.status_code == 200
        assert response.json() == TARIFF_REFUSAL
    with httpx.Client(timeout=5) as client:
        without_session = (
            _confirm(journal, client, origin, None),
            _confirm(journal, client, origin, ""),
            _confirm(journal, client, origin, "not-a-real-token"),
        )
    for response in without_session:
        assert response.status_code == 200
        assert response.json() == REFUSAL
    account = next(row for row in _rows(accounts) if row["email"] == "invalid-token@example.com")
    assert account["email_confirmed_at"] is None
    assert _link(account["id"])["revoked_at"] is None


def _issued_token(origin: str, journal: Journal, before: str, email: str) -> str:
    output = _wait_until(
        journal,
        lambda text: len(_confirmation_lines(text[len(before) :])) >= 1,
    )
    lines = _confirmation_lines(output[len(before) :])
    assert len(lines) == 1
    matched = re.fullmatch(
        rf"{re.escape(origin)}{re.escape(CONFIRM_PATH)}([^ ]+) {re.escape(email)}",
        lines[0],
    )
    assert matched is not None
    return matched.group(1)


def _links_for(account_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(confirmation_links) if row["account_id"] == account_id]


def test_new_confirmation_link_replaces_the_previous_one(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    raw_email = "  Resend Person@Example.COM  "
    email = "resend person@example.com"
    other_email = "other-resend@example.com"
    with httpx.Client(timeout=5) as other:
        other_token = _register(origin, journal, other, other_email)
    other_id = _account_by_email(other_email)["id"]
    other_before = _link(other_id)
    with httpx.Client(timeout=5) as client:
        posted = journal.text()
        registered = _post(
            client,
            origin,
            "/api/register",
            {"email": raw_email, "password": ACCEPTED},
        )
        assert registered.status_code == 200
        assert registered.json()["account"]["email"] == email
        old_token = _issued_token(origin, journal, posted, email)
        account_id = _account_by_email(email)["id"]
        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    sessions.update()
                    .where(sessions.c.account_id == account_id)
                    .values(last_request_at=datetime.now(timezone.utc) - timedelta(hours=1))
                )
        finally:
            engine.dispose()
        seen = next(
            row["last_request_at"]
            for row in _rows(sessions)
            if row["account_id"] == account_id
        )
        before = journal.text()
        replaced = client.post(
            f"{origin}/api/confirmation",
            headers={"Origin": origin},
        )
        assert replaced.status_code == 200
        assert "set-cookie" not in replaced.headers
        body = replaced.json()
        assert body["email_confirmed"] is False
        assert body["period"] is None
        assert body["quota"] is None
        assert body["account"]["email"] == email
        assert body["account"]["id"] == str(account_id)
        new_token = _issued_token(origin, journal, before, email)
        new_output = journal.text()[len(before) :]
        assert new_token != old_token
        assert f"{origin}{CONFIRM_PATH}{new_token} {email}" in new_output
        assert ACCEPTED not in new_output
        assert new_token not in replaced.text
        assert old_token not in replaced.text
        links = _links_for(account_id)
        assert len(links) == 2
        old_link = next(row for row in links if row["token_hash"] == hash_token(old_token))
        new_link = next(row for row in links if row["token_hash"] == hash_token(new_token))
        assert old_link["revoked_at"] is not None
        assert old_link["revoked_at"].utcoffset() == timedelta(0)
        assert new_link["revoked_at"] is None
        assert new_link["expires_at"] > datetime.now(timezone.utc)
        touched = next(
            row["last_request_at"]
            for row in _rows(sessions)
            if row["account_id"] == account_id
        )
        assert touched > seen
        assert abs((datetime.now(timezone.utc) - touched).total_seconds()) < 10
        refused = _confirm(journal, client, origin, old_token)
        assert refused.status_code == 200
        assert refused.json() == TARIFF_REFUSAL
        assert _account(account_id)["email_confirmed_at"] is None
        opened = _confirm(journal, client, origin, new_token)
        assert opened.status_code == 200
        assert opened.json() == {"email_confirmed": True, "destination": "bots"}
    assert _account(account_id)["email_confirmed_at"] is not None
    assert _link(other_id)["revoked_at"] is None
    assert _link(other_id)["token_hash"] == other_before["token_hash"]
    assert other_before["token_hash"] == hash_token(other_token)


def test_confirmation_is_refused_without_origin_session_or_when_confirmed(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "guard-resend@example.com"
    confirmed_email = "already-confirmed@example.com"
    invalid_origin = {
        "code": "invalid_origin",
        "message": "Запрос пришёл не с нашего сервера.",
    }
    unauthenticated = {"code": "unauthenticated", "message": "Нужна сессия."}
    already = {"code": "email_already_confirmed", "message": "Почта уже подтверждена."}
    with httpx.Client(timeout=5) as client:
        _register(origin, journal, client, email)
        account_id = _account_by_email(email)["id"]
        before = journal.text()
        missing_origin = client.post(f"{origin}/api/confirmation")
        foreign = client.post(
            f"{origin}/api/confirmation",
            headers={"Origin": "http://evil.example"},
        )
        assert missing_origin.status_code == 403
        assert foreign.status_code == 403
        assert missing_origin.json() == foreign.json() == invalid_origin
        assert _links_for(account_id) == [_link(account_id)]
        assert _link(account_id)["revoked_at"] is None
    with httpx.Client(timeout=5) as stranger:
        missing_session = stranger.post(
            f"{origin}/api/confirmation",
            headers={"Origin": origin},
        )
    assert missing_session.status_code == 401
    assert missing_session.json() == unauthenticated
    assert len(_links_for(account_id)) == 1
    assert _link(account_id)["revoked_at"] is None
    assert _confirmation_lines(journal.text()[len(before) :]) == []
    with httpx.Client(timeout=5) as client:
        _register(origin, journal, client, confirmed_email)
        confirmed_id = _account_by_email(confirmed_email)["id"]
        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == confirmed_id)
                    .values(email_confirmed_at=datetime(2026, 11, 16, tzinfo=timezone.utc))
                )
        finally:
            engine.dispose()
        marked = journal.text()
        refused = client.post(
            f"{origin}/api/confirmation",
            headers={"Origin": origin},
        )
    assert refused.status_code == 400
    assert refused.json() == already
    assert "set-cookie" not in refused.headers
    assert len(_links_for(confirmed_id)) == 1
    assert _link(confirmed_id)["revoked_at"] is None
    assert _account(confirmed_id)["email_confirmed_at"] == datetime(
        2026, 11, 16, tzinfo=timezone.utc
    )
    assert _confirmation_lines(journal.text()[len(marked) :]) == []
    assert ACCEPTED not in journal.text()[len(before) :]


def test_expired_registration_session_does_not_count_as_current(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    email = "stale-session-confirm@example.com"
    with httpx.Client(timeout=5) as client:
        token = _register(origin, journal, client, email)
        account_id = _account_by_email(email)["id"]
        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    sessions.update()
                    .where(sessions.c.account_id == account_id)
                    .values(last_request_at=datetime.now(timezone.utc) - timedelta(days=31))
                )
        finally:
            engine.dispose()
        opened = _confirm(journal, client, origin, token)
    assert opened.status_code == 200
    assert opened.json() == {"email_confirmed": True, "destination": "login"}
    assert _account(account_id)["email_confirmed_at"] is not None
