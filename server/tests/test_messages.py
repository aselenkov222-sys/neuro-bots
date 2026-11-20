import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.config import development_tariff
from server.db import create_db_engine
from server.models import (
    accounts,
    chats,
    messages,
    quota_charges,
    quota_counters,
    sessions,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
EMPTY_TEXT = "Ответ модели не пришёл."
QUOTA_TEXT = "Квота на период закончилась."
COMPUTER_UNAVAILABLE = "компьютер недоступен"
INVALID_MESSAGE = {
    "code": "invalid_message",
    "message": "Сообщение — от 1 до 8 000 символов.",
}
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
MESSAGE_FIELDS = ("id", "role", "text", "code", "updated_at")


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


def _quota(account_id: uuid.UUID) -> tuple[dict, list[dict]]:
    periods = [
        row for row in _rows(subscription_periods) if row["account_id"] == account_id
    ]
    assert len(periods) == 1
    assert periods[0]["active"] is True
    counter = [
        row for row in _rows(quota_counters) if row["period_id"] == periods[0]["id"]
    ]
    assert len(counter) == 1
    charges = [
        row for row in _rows(quota_charges) if row["period_id"] == periods[0]["id"]
    ]
    return counter[0], charges


def _set_used(account_id: uuid.UUID, used: int) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            period_id = connection.execute(
                select(subscription_periods.c.id).where(
                    subscription_periods.c.account_id == account_id
                )
            ).scalar_one()
            connection.execute(
                quota_counters.update()
                .where(quota_counters.c.period_id == period_id)
                .values(used=used)
            )
    finally:
        engine.dispose()


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _bot(client: TestClient, *, description: str, scenario: str) -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": description, "scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _post_message(client: TestClient, bot_id: str, payload: dict) -> object:
    return client.post(
        f"/api/bots/{bot_id}/messages",
        json=payload,
        headers={"Origin": ORIGIN},
    )


def _chat(bot_id: str) -> dict:
    matched = [row for row in _rows(chats) if row["bot_id"] == uuid.UUID(bot_id)]
    assert len(matched) == 1
    return matched[0]


def _chat_messages(bot_id: str) -> list[dict]:
    chat = _chat(bot_id)
    return [
        row for row in _rows(messages) if row["chat_id"] == chat["id"]
    ]


def test_stub_reply_charges_one_request(database: None) -> None:
    cookie, account_id = _open_ready("stub-message@example.com")
    with _client(cookie) as client:
        bot = _bot(client, description="описание бота", scenario="сценарий бота")
        empty = client.get(f"/api/bots/{bot['id']}/messages")
        assert empty.status_code == 200
        assert empty.json() == {"messages": []}
        posted = _post_message(client, bot["id"], {"text": "  привет  "})
        assert posted.status_code == 200
        assert "set-cookie" not in posted.headers
        body = posted.json()
        assert [tuple(message) for message in body["messages"]] == [MESSAGE_FIELDS, MESSAGE_FIELDS]
        user, assistant = body["messages"]
        assert user["role"] == "user"
        assert user["text"] == "  привет  "
        assert user["code"] is None
        assert assistant["role"] == "assistant"
        assert assistant["text"] == STUB
        assert assistant["code"] is None
        assert "model" not in posted.text
        user_at = datetime.fromisoformat(user["updated_at"])
        assistant_at = datetime.fromisoformat(assistant["updated_at"])
        assert assistant_at - user_at == timedelta(microseconds=1)
        listed = client.get(f"/api/bots/{bot['id']}/messages")
        me = client.get("/api/me")
    assert listed.json() == body
    assert me.status_code == 200
    assert me.json()["quota"] == 499
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    assert charges[0]["created_at"] == user_at
    stored = sorted(
        _chat_messages(bot["id"]),
        key=lambda row: (row["updated_at"], row["id"]),
    )
    assert [row["role"] for row in stored] == ["user", "assistant"]
    assert _chat(bot["id"])["updated_at"] == assistant_at


def test_adapter_receives_the_chat_and_not_the_model_name(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("adapter-call@example.com")
    seen: list[dict] = []

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        seen.append(
            {
                "description": description,
                "scenario": scenario,
                "messages": messages,
                "desktop_available": desktop_available,
                "page_text": page_text,
            }
        )
        return "ответ"

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, description="описание", scenario="")
        posted = _post_message(
            client,
            bot["id"],
            {"text": "первый", "model": "hidden-model", "desktop_available": True},
        )
    assert posted.status_code == 200
    assert len(seen) == 1
    assert set(seen[0]) == {
        "description",
        "scenario",
        "messages",
        "desktop_available",
        "page_text",
    }
    assert seen[0]["description"] == "описание"
    assert seen[0]["scenario"] == ""
    assert seen[0]["desktop_available"] is False
    assert seen[0]["page_text"] == ""
    assert seen[0]["messages"] == [{"role": "user", "text": "первый"}]
    assert "hidden-model" not in str(seen[0])
    assert posted.json()["messages"][1]["text"] == "ответ"
    assert "model" not in posted.text


def test_turn_after_an_error_skips_that_error(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("after-error@example.com")
    replies = iter(["первый ответ", "", "третий"])
    seen: list[list[dict[str, str]]] = []

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        seen.append(messages)
        return next(replies)

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, description="", scenario="ход")
        first = _post_message(client, bot["id"], {"text": "раз"})
        second = _post_message(client, bot["id"], {"text": "два"})
        third = _post_message(client, bot["id"], {"text": "три"})
    assert first.status_code == second.status_code == third.status_code == 200
    assert [message["role"] for message in second.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "error",
    ]
    assert seen[2] == [
        {"role": "user", "text": "раз"},
        {"role": "assistant", "text": "первый ответ"},
        {"role": "user", "text": "два"},
        {"role": "user", "text": "три"},
    ]
    assert all(message["role"] != "error" for message in seen[2])
    assert [message["role"] for message in third.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "error",
        "user",
        "assistant",
    ]


def test_empty_adapter_text_does_not_charge(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("empty-adapter@example.com")

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        return ""

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, description="описание", scenario="сценарий")
        posted = _post_message(client, bot["id"], {"text": "вопрос"})
        me = client.get("/api/me")
    assert posted.status_code == 200
    user, error = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == "вопрос"
    assert error["role"] == "error"
    assert error["text"] == EMPTY_TEXT
    assert error["code"] == "empty_model_response"
    assert [message["role"] for message in posted.json()["messages"]] == ["user", "error"]
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []
    assert me.json()["quota"] == 500
    assert _chat(bot["id"])["updated_at"] == datetime.fromisoformat(error["updated_at"])


def test_exhausted_quota_saves_the_user_and_skips_the_adapter(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("exhausted-quota@example.com")
    _set_used(account_id, development_tariff.model_requests)
    called = False

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        nonlocal called
        called = True
        return "не должен"

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, description="описание", scenario="сценарий")
        posted = _post_message(client, bot["id"], {"text": "ещё"})
    assert posted.status_code == 200
    assert called is False
    user, error = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == "ещё"
    assert error["role"] == "error"
    assert error["text"] == QUOTA_TEXT
    assert error["code"] is None
    counter, charges = _quota(account_id)
    assert counter["used"] == development_tariff.model_requests
    assert charges == []


def test_invalid_message_writes_nothing_and_skips_the_adapter(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("invalid-message@example.com")

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        raise AssertionError("adapter was called")

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, description="", scenario="")
        seen = _rows(sessions)
        rejected = (
            {},
            {"text": ""},
            {"text": "я" * 8001},
            {"text": None},
            {"text": 1},
            {"model": "hidden"},
        )
        for payload in rejected:
            response = _post_message(client, bot["id"], payload)
            assert response.status_code == 400
            assert response.json() == INVALID_MESSAGE
        foreign = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "привет"},
            headers={"Origin": "http://evil.example"},
        )
        missing = client.get(f"/api/bots/{uuid.uuid4()}/messages")
    assert foreign.status_code == 403
    assert foreign.json() == INVALID_ORIGIN
    assert missing.status_code == 404
    assert missing.json() == NOT_FOUND
    assert _chat_messages(bot["id"]) == []
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []
    assert _rows(sessions) == seen


def test_silent_working_desktop_tells_the_chat_and_a_pulse_opens_the_adapter(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("silent-desktop@example.com")
    seen: list[dict] = []

    def complete(*, description: str, scenario: str, messages: list, desktop_available: bool, page_text: str) -> str:
        seen.append(
            {
                "description": description,
                "scenario": scenario,
                "messages": messages,
                "desktop_available": desktop_available,
                "page_text": page_text,
            }
        )
        return "ответ"

    monkeypatch.setattr("server.aggregator.complete", complete)
    working = uuid.uuid4()
    idle = uuid.uuid4()
    with _client(cookie) as client:
        claimed = client.post(
            "/api/desktops",
            json={"id": str(working)},
            headers={"Origin": ORIGIN},
        )
        assert claimed.status_code == 200
        assert claimed.json() == {"id": str(working), "working": True}
        other = client.post(
            "/api/desktops",
            json={"id": str(idle)},
            headers={"Origin": ORIGIN},
        )
        assert other.status_code == 200
        assert other.json()["working"] is False
        bot = _bot(client, description="описание", scenario="сценарий")
        silent = _post_message(client, bot["id"], {"text": "открой сайт"})
        missed = client.post(
            f"/api/desktops/{idle}/pulse",
            json={"timezone": "Asia/Tokyo"},
            headers={"Origin": ORIGIN},
        )
        assert missed.status_code == 404
        still = _post_message(client, bot["id"], {"text": "ещё раз"})
        pulsed = client.post(
            f"/api/desktops/{working}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
        opened = _post_message(
            client,
            bot["id"],
            {"text": "теперь", "desktop_available": False},
        )
        me = client.get("/api/me")
    assert silent.status_code == still.status_code == opened.status_code == 200
    assert [message["role"] for message in silent.json()["messages"]] == [
        "user",
        "error",
    ]
    assert silent.json()["messages"][1]["text"] == COMPUTER_UNAVAILABLE
    assert silent.json()["messages"][1]["code"] is None
    assert still.json()["messages"][3]["text"] == COMPUTER_UNAVAILABLE
    assert still.json()["messages"][3]["role"] == "error"
    assert seen == [
        {
            "description": "описание",
            "scenario": "сценарий",
            "messages": [
                {"role": "user", "text": "открой сайт"},
                {"role": "user", "text": "ещё раз"},
                {"role": "user", "text": "теперь"},
            ],
            "desktop_available": True,
            "page_text": "",
        }
    ]
    assert opened.json()["messages"][-1]["role"] == "assistant"
    assert opened.json()["messages"][-1]["text"] == "ответ"
    assert COMPUTER_UNAVAILABLE not in str(seen)
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    assert me.json()["quota"] == 499
