import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.aggregator import CompleteResult
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    chats,
    library_files,
    messages,
    quota_charges,
    quota_counters,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
EMPTY_TEXT = "Ответ модели не пришёл."
XLSX_NAME = "письма.xlsx"
XLSX_BYTES = b"PK\x03\x04\x00library-xlsx-marker"
REPLY = "Таблица готова."
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
    counter = [
        row for row in _rows(quota_counters) if row["period_id"] == periods[0]["id"]
    ]
    assert len(counter) == 1
    charges = [
        row for row in _rows(quota_charges) if row["period_id"] == periods[0]["id"]
    ]
    return counter[0], charges


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _bot(client: TestClient) -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": "описание", "scenario": "сценарий"},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _library(bot_id: str) -> list[dict]:
    return [
        row
        for row in _rows(library_files)
        if row["bot_id"] == uuid.UUID(bot_id)
    ]


def _main_chat(bot_id: str) -> dict:
    matched = [
        row
        for row in _rows(chats)
        if row["bot_id"] == uuid.UUID(bot_id) and row["kind"] == "main"
    ]
    assert len(matched) == 1
    return matched[0]


def test_xlsx_from_the_adapter_lands_in_the_bot_library(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("library-main@example.com")
    seen: list[list[dict[str, str]]] = []

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
    ) -> CompleteResult:
        seen.append(messages)
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/messages")
        me = client.get("/api/me")
    assert posted.status_code == 200
    user, assistant = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == "собери таблицу"
    assert tuple(user) == MESSAGE_FIELDS
    assert assistant["role"] == "assistant"
    assert assistant["text"] == REPLY
    assert assistant["code"] is None
    assert tuple(assistant) == (*MESSAGE_FIELDS, "file")
    assert tuple(assistant["file"]) == ("id", "name")
    assert assistant["file"]["name"] == XLSX_NAME
    assert XLSX_BYTES not in posted.content
    assert XLSX_NAME not in assistant["text"]
    assert listed.json() == posted.json()
    stored = _library(bot["id"])
    assert len(stored) == 1
    row = stored[0]
    assert str(row["id"]) == assistant["file"]["id"]
    assert row["name"] == XLSX_NAME
    assert bytes(row["content"]) == XLSX_BYTES
    assert row["bot_id"] == uuid.UUID(bot["id"])
    chat = _main_chat(bot["id"])
    assert row["chat_id"] == chat["id"]
    assert row["message_id"] == uuid.UUID(assistant["id"])
    saved = [
        item
        for item in _rows(messages)
        if item["chat_id"] == chat["id"] and item["role"] == "assistant"
    ]
    assert saved[0]["text"] == REPLY
    assert XLSX_BYTES not in saved[0]["text"].encode()
    assert seen == [[{"role": "user", "text": "собери таблицу"}]]
    assert XLSX_BYTES not in str(seen).encode()
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    assert me.json()["quota"] == 499


def test_xlsx_asked_in_an_extra_chat_stays_there(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-extra@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
    ) -> str | CompleteResult:
        if messages[-1]["text"] == "разовое":
            return CompleteResult("файл дополнительного", (XLSX_NAME, XLSX_BYTES))
        return "ответ основного"

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        main = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "в основном"},
            headers={"Origin": ORIGIN},
        )
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        extra_id = created.json()["id"]
        posted = client.post(
            f"/api/bots/{bot['id']}/chats/{extra_id}/messages",
            json={"text": "разовое"},
            headers={"Origin": ORIGIN},
        )
        again = client.get(f"/api/bots/{bot['id']}/messages")
        extra_listed = client.get(
            f"/api/bots/{bot['id']}/chats/{extra_id}/messages"
        )
    assert main.status_code == 200
    assert [row["text"] for row in main.json()["messages"]] == [
        "в основном",
        "ответ основного",
    ]
    assert all("file" not in row for row in main.json()["messages"])
    assert posted.status_code == 200
    extra_user, extra_assistant = posted.json()["messages"]
    assert extra_user["text"] == "разовое"
    assert "file" not in extra_user
    assert extra_assistant["text"] == "файл дополнительного"
    assert extra_assistant["file"]["name"] == XLSX_NAME
    assert [row["text"] for row in again.json()["messages"]] == [
        "в основном",
        "ответ основного",
    ]
    assert all("file" not in row for row in again.json()["messages"])
    assert "файл дополнительного" not in again.text
    assert extra_listed.json() == posted.json()
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert stored[0]["chat_id"] == uuid.UUID(extra_id)
    assert stored[0]["message_id"] == uuid.UUID(extra_assistant["id"])
    assert stored[0]["bot_id"] == uuid.UUID(bot["id"])
    assert bytes(stored[0]["content"]) == XLSX_BYTES
    assert str(stored[0]["id"]) == extra_assistant["file"]["id"]


def test_stub_writes_text_and_no_file(database: None) -> None:
    cookie, account_id = _open_ready("library-stub@example.com")
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "привет"},
            headers={"Origin": ORIGIN},
        )
        me = client.get("/api/me")
    assert posted.status_code == 200
    user, assistant = posted.json()["messages"]
    assert user["text"] == "привет"
    assert tuple(user) == MESSAGE_FIELDS
    assert assistant["role"] == "assistant"
    assert assistant["text"] == STUB
    assert tuple(assistant) == MESSAGE_FIELDS
    assert _library(bot["id"]) == []
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    assert me.json()["quota"] == 499


def test_empty_text_with_xlsx_does_not_charge_or_store(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("library-empty@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
    ) -> CompleteResult:
        return CompleteResult("", (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "вопрос"},
            headers={"Origin": ORIGIN},
        )
        me = client.get("/api/me")
    assert posted.status_code == 200
    user, error = posted.json()["messages"]
    assert user["role"] == "user"
    assert tuple(user) == MESSAGE_FIELDS
    assert error["role"] == "error"
    assert error["text"] == EMPTY_TEXT
    assert error["code"] == "empty_model_response"
    assert tuple(error) == MESSAGE_FIELDS
    assert XLSX_BYTES not in posted.content
    assert _library(bot["id"]) == []
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []
    assert me.json()["quota"] == 500


def test_a_name_that_is_not_xlsx_stores_text_only(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("library-name@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
    ) -> CompleteResult:
        return CompleteResult("Готовый текст.", ("письма.csv", XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "сделай файл"},
            headers={"Origin": ORIGIN},
        )
        me = client.get("/api/me")
    assert posted.status_code == 200
    user, assistant = posted.json()["messages"]
    assert tuple(user) == MESSAGE_FIELDS
    assert assistant["role"] == "assistant"
    assert assistant["text"] == "Готовый текст."
    assert assistant["code"] is None
    assert tuple(assistant) == MESSAGE_FIELDS
    assert XLSX_BYTES not in posted.content
    assert _library(bot["id"]) == []
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    assert me.json()["quota"] == 499
