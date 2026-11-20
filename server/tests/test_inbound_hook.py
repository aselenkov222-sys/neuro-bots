import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    chats,
    messages,
    quota_charges,
    quota_counters,
    routine_runs,
    routines,
    sessions,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
FOREIGN = "http://evil.example"
MARKER = "hook-header-marker"
UNKNOWN = "unknown-hook-token"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
INACTIVE_TEXT = "Подписка неактивна."
PAYLOAD_TOO_LARGE = {
    "code": "payload_too_large",
    "message": "Сообщение длиннее 8 000 символов.",
}
INVALID_BODY = {"code": "invalid_body", "message": "Тело запроса не текст."}
WEBHOOK_UNKNOWN = {"code": "webhook_unknown", "message": "Адрес не найден."}
EMAIL_UNCONFIRMED = {
    "code": "email_unconfirmed",
    "message": "Почта ещё не подтверждена.",
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


def _open_ready(email: str) -> tuple[str, uuid.UUID]:
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
            period_id = uuid.uuid4()
            connection.execute(
                subscription_periods.insert().values(
                    id=period_id,
                    account_id=account_id,
                    started_at=confirmed_at,
                    ends_at=calendar_month_end(confirmed_at),
                    active=True,
                )
            )
            connection.execute(
                quota_counters.insert().values(
                    id=uuid.uuid4(),
                    period_id=period_id,
                    used=0,
                )
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _client(cookie: str | None = None) -> TestClient:
    client = TestClient(app)
    if cookie is not None:
        client.cookies.set("session", cookie)
    return client


def _session(cookie: str) -> dict:
    matched = [row for row in _rows(sessions) if row["id"] == uuid.UUID(cookie)]
    assert len(matched) == 1
    return matched[0]


def _account_sessions(account_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(sessions) if row["account_id"] == account_id]


def _quota(account_id: uuid.UUID) -> tuple[dict, list[dict]]:
    periods = [
        row for row in _rows(subscription_periods) if row["account_id"] == account_id
    ]
    assert len(periods) == 1
    counter = [
        row for row in _rows(quota_counters) if row["period_id"] == periods[0]["id"]
    ]
    assert len(counter) == 1
    charges = [
        row for row in _rows(quota_charges) if row["period_id"] == periods[0]["id"]
    ]
    return counter[0], charges


def _main_chat(bot_id: str) -> dict:
    matched = [
        row
        for row in _rows(chats)
        if row["bot_id"] == uuid.UUID(bot_id) and row["kind"] == "main"
    ]
    assert len(matched) == 1
    return matched[0]


def _chat_messages(bot_id: str) -> list[dict]:
    chat = _main_chat(bot_id)
    rows = [row for row in _rows(messages) if row["chat_id"] == chat["id"]]
    return sorted(rows, key=lambda row: (row["updated_at"], row["id"]))


def _stored_secret(bot_id: str) -> str:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            secret = connection.execute(
                select(bots.c.inbound_secret).where(bots.c.id == uuid.UUID(bot_id))
            ).scalar_one()
    finally:
        engine.dispose()
    return str(secret)


def _bot_routines(bot_id: str) -> list[dict]:
    return [row for row in _rows(routines) if row["bot_id"] == uuid.UUID(bot_id)]


def _runs(routine_ids: set[uuid.UUID]) -> list[dict]:
    return [row for row in _rows(routine_runs) if row["routine_id"] in routine_ids]


def _clear_confirmation(account_id: uuid.UUID) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(email_confirmed_at=None)
            )
    finally:
        engine.dispose()


def _set_period_active(account_id: uuid.UUID, active: bool) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                subscription_periods.update()
                .where(subscription_periods.c.account_id == account_id)
                .values(active=active)
            )
    finally:
        engine.dispose()


def _secret(address: str) -> str:
    prefix = "/api/hooks/"
    assert address.startswith(prefix)
    secret = address.removeprefix(prefix)
    assert secret != "" and "/" not in secret
    return secret


def _hook(
    client: TestClient,
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    origin: str | None = None,
    cookie: str | None = None,
) -> object:
    headers = {"X-Marker": MARKER}
    if origin is not None:
        headers["Origin"] = origin
    if cookie is not None:
        headers["Cookie"] = f"session={cookie}"
    kwargs: dict = {"headers": headers}
    if content is not None:
        kwargs["content"] = content
    return client.request(method, url, **kwargs)


def test_hook_writes_the_main_chat_without_a_session_or_origin(
    database: None,
) -> None:
    cookie, account_id = _open_ready("hook-turn@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра", "description": "описание", "scenario": "сценарий"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    secret = _secret(created.json()["inbound_webhook"])
    path = f"/api/hooks/{secret}"
    seen = _session(cookie)["last_request_at"]
    assert len(_account_sessions(account_id)) == 1
    cases = (
        ("GET", f"{path}?a=1&b=2", None, None, None, "GET\na=1&b=2"),
        ("POST", f"{path}?a=1&b=2", b"hello", FOREIGN, None, "POST\na=1&b=2\nhello"),
        ("GET", path, None, FOREIGN, None, "GET"),
        ("POST", path, b"", "", None, "POST"),
        ("GET", path, b"ping", "", None, "GET\nping"),
        ("POST", path, b"hello", None, None, "POST\nhello"),
        ("POST", f"{path}?a=1&b=2", b"", None, None, "POST\na=1&b=2"),
        ("GET", f"{path}?", None, None, None, "GET"),
        ("POST", f"{path}?", b"hello", None, None, "POST\nhello"),
        ("GET", f"{path}?q=a%2Fb", None, None, None, "GET\nq=a%2Fb"),
        ("POST", path, None, None, cookie, "POST"),
    )
    with _client() as caller:
        for method, url, content, origin, sent_cookie, _expected in cases:
            response = _hook(
                caller,
                method,
                url,
                content=content,
                origin=origin,
                cookie=sent_cookie,
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert STUB not in response.text
            assert MARKER not in response.text
            assert "set-cookie" not in response.headers
    stored = _chat_messages(bot_id)
    assert [row["role"] for row in stored] == ["user", "assistant"] * len(cases)
    assert [row["text"] for row in stored if row["role"] == "user"] == [
        expected for *_rest, expected in cases
    ]
    assert all(row["text"] == STUB for row in stored if row["role"] == "assistant")
    assert all(row["code"] is None for row in stored)
    assert all(MARKER not in row["text"] for row in stored)
    assert all("\n\n" not in row["text"] for row in stored)
    assert all(not row["text"].endswith("\n") for row in stored)
    assert _main_chat(bot_id)["kind"] == "main"
    counter, charges = _quota(account_id)
    assert counter["used"] == len(cases)
    assert len(charges) == len(cases)
    assert _session(cookie)["last_request_at"] == seen
    assert len(_account_sessions(account_id)) == 1
    before_messages = len(stored)
    with _client() as caller:
        unknown = _hook(caller, "GET", f"/api/hooks/{UNKNOWN}")
    assert unknown.status_code == 404
    assert unknown.json() == WEBHOOK_UNKNOWN
    assert tuple(unknown.json()) == ("code", "message")
    assert UNKNOWN not in unknown.text
    assert len(_chat_messages(bot_id)) == before_messages
    with _client(cookie) as owner:
        posted = owner.post(
            f"/api/bots/{bot_id}/messages",
            json={"text": "обычная реплика"},
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    assert posted.json()["messages"][-1]["text"] == STUB
    assert posted.json()["messages"][-2]["text"] == "обычная реплика"
    assert _session(cookie)["last_request_at"] > seen


def test_hook_does_not_create_or_start_a_routine_and_pause_does_not_stop_it(
    database: None,
) -> None:
    cookie, account_id = _open_ready("hook-routine@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра", "scenario": "план"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    path = created.json()["inbound_webhook"]
    with _client() as caller:
        first = _hook(caller, "POST", path, content=b"hello")
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert _bot_routines(bot_id) == []
    assert _rows(routine_runs) == []
    spoken = _chat_messages(bot_id)
    assert [row["role"] for row in spoken] == ["user", "assistant"]
    assert spoken[0]["text"] == "POST\nhello"
    assert spoken[1]["text"] == STUB
    with _client(cookie) as owner:
        routine = owner.post(
            f"/api/bots/{bot_id}/routines",
            json={"weekdays": [1, 3, 5], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        assert routine.status_code == 200
        assert routine.json()["paused"] is False
        routine_id = routine.json()["id"]
        paused = owner.patch(
            f"/api/bots/{bot_id}/routines/{routine_id}",
            json={"paused": True},
            headers={"Origin": ORIGIN},
        )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    stored = _bot_routines(bot_id)
    assert [str(row["id"]) for row in stored] == [routine_id]
    assert stored[0]["paused"] is True
    assert _runs({uuid.UUID(routine_id)}) == []
    before = _quota(account_id)
    with _client() as caller:
        second = _hook(caller, "GET", path)
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert STUB not in second.text
    after = _bot_routines(bot_id)
    assert [str(row["id"]) for row in after] == [routine_id]
    assert after[0]["paused"] is True
    assert _runs({uuid.UUID(routine_id)}) == []
    again = _chat_messages(bot_id)
    assert [row["role"] for row in again] == ["user", "assistant", "user", "assistant"]
    assert again[2]["text"] == "GET"
    assert again[3]["text"] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == before[0]["used"] + 1
    assert len(charges) == len(before[1]) + 1


def test_inactive_subscription_accepts_the_hook_without_the_model(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_complete(**kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", fail_complete)
    cookie, account_id = _open_ready("hook-inactive@example.com")
    _set_period_active(account_id, False)
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра", "scenario": "план"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        bot_id = created.json()["id"]
        path = created.json()["inbound_webhook"]
        posted = owner.post(
            f"/api/bots/{bot_id}/messages",
            json={"text": "обычная реплика"},
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    ordinary = posted.json()["messages"]
    assert [row["role"] for row in ordinary] == ["user", "error"]
    assert ordinary[1]["text"] == INACTIVE_TEXT
    assert ordinary[1]["code"] is None
    with _client() as caller:
        hooked = _hook(caller, "POST", path, content=b"hello")
    assert hooked.status_code == 200
    assert hooked.json() == {"ok": True}
    assert INACTIVE_TEXT not in hooked.text
    assert STUB not in hooked.text
    stored = _chat_messages(bot_id)
    assert [row["role"] for row in stored] == ["user", "error", "user", "error"]
    assert stored[0]["text"] == "обычная реплика"
    assert stored[2]["text"] == "POST\nhello"
    assert stored[1]["role"] == stored[3]["role"] == "error"
    assert stored[1]["text"] == stored[3]["text"] == INACTIVE_TEXT
    assert stored[1]["code"] is None and stored[3]["code"] is None
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []
    assert _bot_routines(bot_id) == []
    assert _rows(routine_runs) == []


def test_hook_body_of_8000_characters_is_accepted_and_longer_or_non_utf8_is_refused(
    database: None,
) -> None:
    cookie, account_id = _open_ready("hook-body@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    path = created.json()["inbound_webhook"]
    accepted_body = "я" * 8000
    with _client() as caller:
        accepted = _hook(
            caller, "POST", f"{path}?q=1", content=accepted_body.encode("utf-8")
        )
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    spoken = _chat_messages(bot_id)
    assert [row["role"] for row in spoken] == ["user", "assistant"]
    assert spoken[0]["text"] == "POST\nq=1\n" + accepted_body
    assert len(spoken[0]["text"]) > 8000
    assert spoken[1]["text"] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    with _client() as caller:
        too_long = _hook(
            caller, "POST", path, content=("я" * 8001).encode("utf-8")
        )
        invalid = _hook(caller, "POST", path, content=b"\xff")
        plain_unknown = caller.get(f"/api/hooks/{UNKNOWN}")
        bad_unknown = caller.request(
            "POST", f"/api/hooks/{UNKNOWN}", content=b"\xff"
        )
    assert too_long.status_code == 400
    assert too_long.json() == PAYLOAD_TOO_LARGE
    assert tuple(too_long.json()) == ("code", "message")
    assert invalid.status_code == 400
    assert invalid.json() == INVALID_BODY
    assert tuple(invalid.json()) == ("code", "message")
    assert STUB not in too_long.text
    assert STUB not in invalid.text
    assert _chat_messages(bot_id) == spoken
    again, again_charges = _quota(account_id)
    assert again["used"] == counter["used"]
    assert len(again_charges) == len(charges)
    assert bad_unknown.status_code == plain_unknown.status_code == 404
    assert bad_unknown.json() == plain_unknown.json() == WEBHOOK_UNKNOWN
    assert bad_unknown.content == plain_unknown.content
    assert UNKNOWN not in bad_unknown.text


def test_unknown_address_and_unconfirmed_email_do_not_start_a_turn(
    database: None,
) -> None:
    cookie, account_id = _open_ready("hook-unconfirmed@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    path = created.json()["inbound_webhook"]
    _clear_confirmation(account_id)
    messages_before = len(_rows(messages))
    charges_before = len(_rows(quota_charges))
    long_body = ("я" * 8001).encode("utf-8")
    with _client() as caller:
        unknown_get = _hook(caller, "GET", f"/api/hooks/{UNKNOWN}")
        unknown_long = _hook(
            caller, "POST", f"/api/hooks/{UNKNOWN}", content=long_body
        )
        unknown_binary = _hook(
            caller, "POST", f"/api/hooks/{UNKNOWN}", content=b"\xff"
        )
        unconfirmed = _hook(caller, "POST", path, content=b"hello")
        unconfirmed_long = _hook(caller, "POST", path, content=long_body)
        unconfirmed_binary = _hook(caller, "POST", path, content=b"\xff")
    for response in (unknown_get, unknown_long, unknown_binary):
        assert response.status_code == 404
        assert response.json() == WEBHOOK_UNKNOWN
        assert tuple(response.json()) == ("code", "message")
        assert UNKNOWN not in response.text
        assert STUB not in response.text
    for response in (unconfirmed, unconfirmed_long, unconfirmed_binary):
        assert response.status_code == 403
        assert response.json() == EMAIL_UNCONFIRMED
        assert tuple(response.json()) == ("code", "message")
        assert STUB not in response.text
    assert _chat_messages(bot_id) == []
    assert len(_rows(messages)) == messages_before
    assert len(_rows(quota_charges)) == charges_before
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []


def test_two_accepted_hooks_are_separate_turns(database: None) -> None:
    cookie, account_id = _open_ready("hook-two-turns@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    path = created.json()["inbound_webhook"]
    with _client() as caller:
        first = _hook(caller, "POST", path, content=b"one")
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert tuple(first.json()) == ("ok",)
    assert STUB not in first.text
    after_first = _chat_messages(bot_id)
    assert [row["role"] for row in after_first] == ["user", "assistant"]
    assert after_first[0]["text"] == "POST\none"
    assert after_first[1]["text"] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    with _client() as caller:
        second = _hook(caller, "GET", path)
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert tuple(second.json()) == ("ok",)
    assert STUB not in second.text
    stored = _chat_messages(bot_id)
    assert [row["role"] for row in stored] == ["user", "assistant", "user", "assistant"]
    assert stored[2]["text"] == "GET"
    assert stored[3]["text"] == STUB
    assert stored[1]["text"] == stored[3]["text"] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == 2
    assert len(charges) == 2


class _Journal:
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


def _stop(process: subprocess.Popen[str], journal: _Journal) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    journal.join()


def _wait_until(journal: _Journal, predicate, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = journal.text()
        if predicate(current):
            return current
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    return journal.text()


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def test_hook_request_does_not_write_the_address_to_the_server_journal(
    database: None,
) -> None:
    cookie, _account_id = _open_ready("hook-journal@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Журнал"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    secret = _secret(created.json()["inbound_webhook"])
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
    journal = _Journal(process)
    try:
        with httpx.Client(timeout=5) as client:
            ready = _wait_until(
                journal,
                lambda current: "Application startup complete." in current
                and _health_ok(client, origin),
            )
            assert "Application startup complete." in ready, ready
            posted = client.post(
                f"{origin}/api/hooks/{secret}?a=1&b=2",
                content=b"hello",
                headers={"Origin": FOREIGN, "X-Marker": MARKER},
            )
            empty = client.get(f"{origin}/api/hooks/{secret}")
            unknown = client.get(f"{origin}/api/hooks/{UNKNOWN}")
        assert posted.status_code == 200
        assert posted.json() == {"ok": True}
        assert empty.status_code == 200
        assert empty.json() == {"ok": True}
        assert unknown.status_code == 404
        assert unknown.json() == WEBHOOK_UNKNOWN
        assert UNKNOWN not in unknown.text
        time.sleep(0.5)
        written = journal.text()
        assert secret not in written
        assert "/api/hooks/" not in written
        assert UNKNOWN not in written
    finally:
        _stop(process, journal)


def test_rotate_replaces_the_address_and_ignores_a_client_secret(
    database: None,
) -> None:
    cookie, account_id = _open_ready("hook-rotate@example.com")
    seen = _session(cookie)["last_request_at"]
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        bot_id = created.json()["id"]
        old_path = created.json()["inbound_webhook"]
        previous = created.json()["updated_at"]
        rejected = owner.post(
            f"/api/bots/{bot_id}/inbound-webhook",
            json={"inbound_secret": "client-supplied-secret"},
            headers={"Origin": FOREIGN},
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "invalid_origin"
        kept = owner.get(f"/api/bots/{bot_id}")
        assert kept.status_code == 200
        assert kept.json()["inbound_webhook"] == old_path
        rotated = owner.post(
            f"/api/bots/{bot_id}/inbound-webhook",
            json={
                "inbound_secret": "client-supplied-secret",
                "inbound_webhook": "/api/hooks/client-supplied-secret",
            },
            headers={"Origin": ORIGIN},
        )
    assert rotated.status_code == 200
    body = rotated.json()
    assert tuple(body) == (
        "id",
        "name",
        "color",
        "icon",
        "description",
        "scenario",
        "updated_at",
        "chief",
        "inbound_webhook",
    )
    assert body["name"] == "Искра"
    assert body["updated_at"] != previous
    new_path = body["inbound_webhook"]
    assert new_path != old_path
    assert "client-supplied-secret" not in new_path
    stored = _stored_secret(bot_id)
    assert stored != "client-supplied-secret"
    assert new_path == f"/api/hooks/{stored}"
    assert _session(cookie)["last_request_at"] > seen
    with _client(cookie) as owner:
        again = owner.get(f"/api/bots/{bot_id}")
    assert again.json() == body
    with _client() as caller:
        dead = _hook(caller, "POST", old_path, content=b"old")
        live = _hook(caller, "POST", new_path, content=b"new")
    assert dead.status_code == 404
    assert dead.json() == WEBHOOK_UNKNOWN
    assert tuple(dead.json()) == ("code", "message")
    assert live.status_code == 200
    assert live.json() == {"ok": True}
    assert STUB not in live.text
    spoken = _chat_messages(bot_id)
    assert [row["role"] for row in spoken] == ["user", "assistant"]
    assert spoken[0]["text"] == "POST\nnew"
    assert spoken[1]["text"] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1


def test_bot_card_shows_the_absolute_address_and_can_copy_and_rotate(
    database: None,
) -> None:
    cookie, _account_id = _open_ready("hook-card@example.com")
    with _client(cookie) as owner:
        created = owner.post(
            "/api/bots",
            json={"name": "Искра"},
            headers={"Origin": ORIGIN},
        )
    assert created.status_code == 200
    bot_id = created.json()["id"]
    path = created.json()["inbound_webhook"]
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
    journal = _Journal(process)
    fresh = ""
    try:
        with httpx.Client(timeout=5) as client:
            ready = _wait_until(
                journal,
                lambda current: "Application startup complete." in current
                and _health_ok(client, origin),
            )
            assert "Application startup complete." in ready, ready
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context()
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=origin
            )
            context.add_cookies(
                [{"name": "session", "value": cookie, "url": origin}]
            )
            page = context.new_page()
            page.goto(f"{origin}/#bots")
            page.wait_for_selector(".bot-card")
            page.click(".bot-card")
            page.wait_for_selector("#inbound-webhook")
            shown = page.locator("#inbound-webhook").inner_text()
            assert shown == f"{origin}{path}"
            assert page.locator("#inbound-webhook").is_visible()
            assert page.locator("input#inbound-webhook").count() == 0
            assert page.locator("textarea#inbound-webhook").count() == 0
            assert page.locator("h2", has_text="Входящий адрес").count() == 1
            copy = page.locator('[data-action="copy-inbound"]')
            rotate = page.locator('[data-action="rotate-inbound"]')
            assert copy.inner_text() == "Скопировать"
            assert "btn-quiet" in (copy.get_attribute("class") or "")
            assert rotate.inner_text() == "Сменить"
            assert "btn-orange" in (rotate.get_attribute("class") or "")
            assert page.locator(".proto-nav").count() == 0
            assert page.evaluate(
                """() => {
                  const identity = document.querySelector(".identity");
                  const address = document.getElementById("inbound-webhook");
                  const scenario = document.getElementById("scenario");
                  const after = Node.DOCUMENT_POSITION_FOLLOWING;
                  return Boolean(
                    identity && address && scenario &&
                    (identity.compareDocumentPosition(address) & after) &&
                    (address.compareDocumentPosition(scenario) & after)
                  );
                }"""
            )
            copy.click()
            page.wait_for_function(
                "(expected) => navigator.clipboard.readText().then((text) => text === expected)",
                arg=shown,
            )
            rotate.click()
            page.wait_for_function(
                "(previous) => { const node = document.getElementById('inbound-webhook'); return Boolean(node && node.textContent !== previous); }",
                arg=shown,
            )
            with _client(cookie) as owner:
                card = owner.get(f"/api/bots/{bot_id}")
            assert card.status_code == 200
            fresh = card.json()["inbound_webhook"]
            assert fresh != path
            assert page.locator("#inbound-webhook").inner_text() == f"{origin}{fresh}"
            shot = Path("/opt/cursor/artifacts/inbound-card.png")
            if shot.parent.is_dir():
                page.screenshot(path=str(shot), full_page=True)
            browser.close()
        written = journal.text()
        assert path not in written
        assert fresh not in written
        assert "/api/hooks/" not in written
    finally:
        _stop(process, journal)
