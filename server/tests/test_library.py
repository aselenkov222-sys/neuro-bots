import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.aggregator import CompleteResult
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    chats,
    library_files,
    messages,
    routines,
    sessions,
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
FILE_FIELDS = ("id", "name", "created_at")
SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SECOND_NAME = "отчёт.xlsx"
SECOND_BYTES = b"PK\x03\x04\x00second-sheet"
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
FILE_MISSING = {"code": "not_found", "message": "Файл не найден."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
UNCONFIRMED = {"code": "email_unconfirmed", "message": "Почта ещё не подтверждена."}
MAIN_KEPT = {"code": "main_chat", "message": "Основной чат удалить нельзя."}


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
        page_text: str,
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
        page_text: str,
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
        page_text: str,
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
        page_text: str,
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


def test_analysis_comes_back_as_text_without_a_file(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-analysis@example.com")
    reply = "В письме одна просьба и срок до пятницы."

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str:
        return reply

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "Сделай анализ этого письма."},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/messages")
    assert posted.status_code == 200
    user, assistant = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == "Сделай анализ этого письма."
    assert "file" not in user
    assert assistant["role"] == "assistant"
    assert assistant["text"] == reply
    assert assistant["text"] != STUB
    assert "file" not in assistant
    assert tuple(assistant) == MESSAGE_FIELDS
    assert listed.json() == posted.json()
    assert _library(bot["id"]) == []


def test_translation_comes_back_as_text_without_a_file(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-translation@example.com")
    reply = "Please send the letter by Friday."

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str:
        return reply

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "Переведи это письмо на английский."},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/messages")
    assert posted.status_code == 200
    user, assistant = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == "Переведи это письмо на английский."
    assert "file" not in user
    assert assistant["role"] == "assistant"
    assert assistant["text"] == reply
    assert assistant["text"] != STUB
    assert "file" not in assistant
    assert tuple(assistant) == MESSAGE_FIELDS
    assert listed.json() == posted.json()
    assert _library(bot["id"]) == []


def _disposition_name(header: str) -> str:
    prefix = "attachment; filename*=UTF-8''"
    assert header.startswith(prefix)
    return unquote(header.removeprefix(prefix))


def _open_unconfirmed(email: str) -> str:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
    finally:
        engine.dispose()
    return str(session_id)


def test_user_opens_the_stored_xlsx(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-open@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        if messages[-1]["text"] == "ещё таблица":
            return CompleteResult("Второй лист.", (SECOND_NAME, SECOND_BYTES))
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        extra = client.post(
            f"/api/bots/{bot['id']}/chats/{created.json()['id']}/messages",
            json={"text": "ещё таблица"},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/library")
        first = client.get(
            f"/api/bots/{bot['id']}/library/{listed.json()['files'][0]['id']}"
        )
        second = client.get(
            f"/api/bots/{bot['id']}/library/{listed.json()['files'][1]['id']}"
        )
    assert posted.status_code == 200
    assert extra.status_code == 200
    assert listed.status_code == 200
    files = listed.json()["files"]
    assert [tuple(item) for item in files] == [FILE_FIELDS, FILE_FIELDS]
    assert [item["name"] for item in files] == [XLSX_NAME, SECOND_NAME]
    assert XLSX_BYTES not in listed.content
    assert SECOND_BYTES not in listed.content
    assert files[0]["created_at"] <= files[1]["created_at"]
    assert first.status_code == 200
    assert first.headers["content-type"] == SHEET
    assert _disposition_name(first.headers["content-disposition"]) == XLSX_NAME
    assert first.content == XLSX_BYTES
    assert second.status_code == 200
    assert second.headers["content-type"] == SHEET
    assert _disposition_name(second.headers["content-disposition"]) == SECOND_NAME
    assert second.content == SECOND_BYTES
    stored = _library(bot["id"])
    assert {row["name"] for row in stored} == {XLSX_NAME, SECOND_NAME}
    assert {bytes(row["content"]) for row in stored} == {XLSX_BYTES, SECOND_BYTES}


def test_user_deletes_the_file_and_the_chat_text_stays(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("library-delete@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        file_id = posted.json()["messages"][1]["file"]["id"]
        before = client.get("/api/me")
        removed = client.delete(
            f"/api/bots/{bot['id']}/library/{file_id}",
            headers={"Origin": ORIGIN},
        )
        messages = client.get(f"/api/bots/{bot['id']}/messages")
        library = client.get(f"/api/bots/{bot['id']}/library")
        missing = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
        after = client.get("/api/me")
    assert removed.status_code == 204
    assert removed.content == b""
    user, assistant = messages.json()["messages"]
    assert user["text"] == "собери таблицу"
    assert assistant["text"] == REPLY
    assert "file" not in user
    assert "file" not in assistant
    assert tuple(assistant) == MESSAGE_FIELDS
    assert library.json() == {"files": []}
    assert missing.status_code == 404
    assert missing.json() == FILE_MISSING
    assert _library(bot["id"]) == []
    assert before.json()["quota"] == after.json()["quota"] == 499
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1


def test_upload_does_not_create_a_library_file(database: None) -> None:
    cookie, _account_id = _open_ready("library-upload@example.com")
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/library",
            json={"name": "свой.xlsx", "content": "свои байты"},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/library")
    assert posted.status_code == 405
    assert listed.json() == {"files": []}
    assert _library(bot["id"]) == []


def test_a_later_reply_does_not_delete_the_file(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-keep@example.com")
    calls: list[str] = []

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str | CompleteResult:
        calls.append(messages[-1]["text"])
        if messages[-1]["text"] == "собери таблицу":
            return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))
        if messages[-1]["text"] == "разбери текст":
            return "Только разбор."
        return CompleteResult("Второй лист.", (SECOND_NAME, SECOND_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        first = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        file_id = first.json()["messages"][1]["file"]["id"]
        follow = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "разбери текст"},
            headers={"Origin": ORIGIN},
        )
        kept = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
        again = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "ещё таблица"},
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/library")
        still = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
    assert follow.json()["messages"][1]["file"]["id"] == file_id
    assert follow.json()["messages"][3]["text"] == "Только разбор."
    assert "file" not in follow.json()["messages"][3]
    assert kept.content == XLSX_BYTES
    assert [item["name"] for item in listed.json()["files"]] == [
        XLSX_NAME,
        SECOND_NAME,
    ]
    assert still.content == XLSX_BYTES
    assert again.json()["messages"][-1]["file"]["name"] == SECOND_NAME
    assert calls == ["собери таблицу", "разбери текст", "ещё таблица"]


def test_another_account_cannot_open_or_delete_the_file(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-owner@example.com")
    other, _other_id = _open_ready("library-stranger@example.com")
    plain = _open_unconfirmed("library-plain@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        file_id = posted.json()["messages"][1]["file"]["id"]
    with _client(other) as client:
        listed = client.get(f"/api/bots/{bot['id']}/library")
        opened = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
        removed = client.delete(
            f"/api/bots/{bot['id']}/library/{file_id}",
            headers={"Origin": ORIGIN},
        )
    with _client(plain) as client:
        unconfirmed = client.get(f"/api/bots/{bot['id']}/library")
    with TestClient(app) as anonymous:
        missing = anonymous.get(f"/api/bots/{bot['id']}/library")
    assert listed.status_code == 404
    assert listed.json() == NOT_FOUND
    assert opened.status_code == 404
    assert opened.json() == NOT_FOUND
    assert removed.status_code == 404
    assert removed.json() == NOT_FOUND
    assert unconfirmed.status_code == 403
    assert unconfirmed.json() == UNCONFIRMED
    assert missing.status_code == 401
    assert missing.json() == UNAUTHENTICATED
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert bytes(stored[0]["content"]) == XLSX_BYTES


def test_delete_without_origin_keeps_the_file(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-origin@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        file_id = posted.json()["messages"][1]["file"]["id"]
        refused = client.delete(f"/api/bots/{bot['id']}/library/{file_id}")
        opened = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
    assert refused.status_code == 403
    assert refused.json() == INVALID_ORIGIN
    assert opened.content == XLSX_BYTES
    assert len(_library(bot["id"])) == 1


def _session_cookie(response) -> str:
    pair = response.headers["set-cookie"].split(";", 1)[0]
    name, value = pair.split("=", 1)
    assert name == "session"
    uuid.UUID(value)
    return value


def _vault_catalog_path() -> Path:
    configured = os.environ.get("NEURO_BOTS_STATE_DIR")
    if configured:
        base = Path(configured)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "neuro-bots"
    else:
        base = Path.home() / ".local" / "share" / "neuro-bots"
    return base / "vault.json"


def _vault_catalog_bytes() -> bytes | None:
    path = _vault_catalog_path()
    if not path.is_file():
        return None
    return path.read_bytes()


def test_another_login_downloads_the_same_xlsx_and_the_vault_stays_clear(
    database: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    email = "library-second@example.com"
    cookie, account_id = _open_ready(email)

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    file_id = posted.json()["messages"][1]["file"]["id"]
    with TestClient(app) as fresh:
        logged = fresh.post(
            "/api/login",
            json={"email": email, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
    assert logged.status_code == 200
    second = _session_cookie(logged)
    assert second != cookie
    owned = [
        row for row in _rows(sessions) if row["account_id"] == account_id
    ]
    assert {str(row["id"]) for row in owned} == {cookie, second}
    catalog_before = _vault_catalog_bytes()
    with caplog.at_level(logging.DEBUG):
        with _client(second) as other:
            listed = other.get(f"/api/bots/{bot['id']}/library")
            opened = other.get(f"/api/bots/{bot['id']}/library/{file_id}")
        with _client(cookie) as first:
            signed_out = first.post("/api/logout", headers={"Origin": ORIGIN})
        with _client(second) as other:
            again = other.get(f"/api/bots/{bot['id']}/library/{file_id}")
        with _client(cookie) as first:
            closed = first.get(f"/api/bots/{bot['id']}/library/{file_id}")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["files"]] == [XLSX_NAME]
    assert listed.json()["files"][0]["id"] == file_id
    assert XLSX_BYTES not in listed.content
    assert opened.status_code == 200
    assert opened.content == XLSX_BYTES
    assert opened.headers["content-type"].startswith(SHEET)
    assert "set-cookie" not in opened.headers
    assert signed_out.status_code == 200
    assert again.status_code == 200
    assert again.content == XLSX_BYTES
    assert closed.status_code == 401
    assert closed.json() == UNAUTHENTICATED
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert stored[0]["name"] == XLSX_NAME
    assert bytes(stored[0]["content"]) == XLSX_BYTES
    saved = sorted(
        (
            row
            for row in _rows(messages)
            if row["chat_id"] == stored[0]["chat_id"]
        ),
        key=lambda row: (row["updated_at"], row["role"] != "user"),
    )
    assert [row["text"] for row in saved] == ["собери таблицу", REPLY]
    assert all(XLSX_BYTES not in row["text"].encode() for row in saved)
    remaining = [
        row for row in _rows(sessions) if row["account_id"] == account_id
    ]
    assert [str(row["id"]) for row in remaining] == [second]
    catalog_after = _vault_catalog_bytes()
    assert catalog_after == catalog_before
    catalog = catalog_after or b""
    assert XLSX_NAME.encode() not in catalog
    assert XLSX_BYTES not in catalog
    assert XLSX_NAME not in second
    assert XLSX_BYTES not in second.encode()
    journal = caplog.text.encode()
    assert XLSX_BYTES not in journal
    assert XLSX_NAME.encode() not in journal
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1


def test_chat_screen_offers_open_and_delete() -> None:
    script = (PROJECT_ROOT.parent / "web" / "app.js").read_text()
    styles = (PROJECT_ROOT.parent / "web" / "styles.css").read_text()
    screen = script.split("function fileChip(", 1)[1].split("function render(", 1)[0]
    assert "<h2>Библиотека</h2>" in screen
    assert ">Открыть</button>" in screen
    assert ">Удалить</button>" in screen
    assert "Файлов пока нет." in screen
    assert 'class="inbox"' in screen
    assert "roster" not in screen
    assert 'type="file"' not in script
    assert "grid-template-columns: minmax(0, 1fr) 228px;" in styles
    assert ".home .library" in styles
    assert ".home .file" in styles
    stacked = styles.split(".home .library .file > .btn-quiet", 1)[1].split("}", 1)[0]
    assert "grid-column: 1 / -1" in stacked


def test_deleting_an_extra_chat_keeps_its_xlsx(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-extra-keep@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str | CompleteResult:
        if messages[-1]["text"] == "таблица в дополнительном":
            return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))
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
            json={"text": "таблица в дополнительном"},
            headers={"Origin": ORIGIN},
        )
        before = client.get(f"/api/bots/{bot['id']}/messages")
        removed = client.delete(
            f"/api/bots/{bot['id']}/chats/{extra_id}",
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/library")
        file_id = listed.json()["files"][0]["id"]
        opened = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
        gone = client.get(f"/api/bots/{bot['id']}/chats/{extra_id}/messages")
        again = client.get(f"/api/bots/{bot['id']}/messages")
    assert main.status_code == 200
    assert posted.status_code == 200
    assert posted.json()["messages"][1]["file"]["name"] == XLSX_NAME
    assert removed.status_code == 204
    assert removed.content == b""
    assert before.json()["messages"][0]["text"] == "в основном"
    assert before.json()["messages"][1]["text"] == "ответ основного"
    assert again.json() == before.json()
    assert gone.status_code == 404
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["files"]] == [XLSX_NAME]
    assert opened.status_code == 200
    assert opened.content == XLSX_BYTES
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert stored[0]["name"] == XLSX_NAME
    assert bytes(stored[0]["content"]) == XLSX_BYTES
    assert stored[0]["chat_id"] is None
    assert stored[0]["message_id"] is None
    extra_messages = [
        row for row in _rows(messages) if row["chat_id"] == uuid.UUID(extra_id)
    ]
    assert extra_messages == []
    assert [row for row in _rows(chats) if row["id"] == uuid.UUID(extra_id)] == []


def test_refusing_to_delete_the_main_chat_keeps_the_xlsx(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-main-kept@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        chats_body = client.get(f"/api/bots/{bot['id']}/chats")
        main_id = chats_body.json()["chats"][0]["id"]
        refused = client.delete(
            f"/api/bots/{bot['id']}/chats/{main_id}",
            headers={"Origin": ORIGIN},
        )
        listed = client.get(f"/api/bots/{bot['id']}/library")
        file_id = posted.json()["messages"][1]["file"]["id"]
        opened = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
        transcript = client.get(f"/api/bots/{bot['id']}/messages")
    assert refused.status_code == 400
    assert refused.json() == MAIN_KEPT
    assert listed.status_code == 200
    assert listed.json()["files"][0]["id"] == file_id
    assert opened.content == XLSX_BYTES
    assert [row["text"] for row in transcript.json()["messages"]] == [
        "собери таблицу",
        REPLY,
    ]
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert bytes(stored[0]["content"]) == XLSX_BYTES
    assert stored[0]["chat_id"] == uuid.UUID(main_id)


def test_deleting_the_bot_removes_its_library_and_leaves_the_other(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("library-bot-delete@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        if messages[-1]["text"] == "вторая таблица":
            return CompleteResult("Второй лист.", (SECOND_NAME, SECOND_BYTES))
        return CompleteResult(REPLY, (XLSX_NAME, b"PK\x03\x04\x00deleted-bot-xlsx"))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        first = _bot(client)
        posted = client.post(
            f"/api/bots/{first['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        routine = client.post(
            f"/api/bots/{first['id']}/routines",
            json={"weekdays": [1], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        second = client.post(
            "/api/bots",
            json={"name": "Второй", "description": "описание", "scenario": "сценарий"},
            headers={"Origin": ORIGIN},
        )
        other = client.post(
            f"/api/bots/{second.json()['id']}/messages",
            json={"text": "вторая таблица"},
            headers={"Origin": ORIGIN},
        )
        before = client.get("/api/me")
        removed = client.delete(
            f"/api/bots/{first['id']}",
            headers={"Origin": ORIGIN},
        )
        gone = client.get(f"/api/bots/{first['id']}")
        kept = client.get(
            f"/api/bots/{second.json()['id']}/library/{other.json()['messages'][1]['file']['id']}"
        )
        after = client.get("/api/me")
    assert posted.status_code == 200
    assert routine.status_code == 200
    assert second.status_code == 200
    assert other.status_code == 200
    assert removed.status_code == 204
    assert removed.content == b""
    assert gone.status_code == 404
    assert gone.json() == NOT_FOUND
    assert kept.status_code == 200
    assert kept.content == SECOND_BYTES
    first_id = uuid.UUID(first["id"])
    second_id = uuid.UUID(second.json()["id"])
    assert [row for row in _rows(bots) if row["id"] == first_id] == []
    assert [row for row in _rows(routines) if row["bot_id"] == first_id] == []
    assert [row for row in _rows(chats) if row["bot_id"] == first_id] == []
    remaining = _rows(library_files)
    assert [row for row in remaining if row["bot_id"] == first_id] == []
    assert b"PK\x03\x04\x00deleted-bot-xlsx" not in {
        bytes(row["content"]) for row in remaining
    }
    kept_rows = [row for row in remaining if row["bot_id"] == second_id]
    assert len(kept_rows) == 1
    assert kept_rows[0]["name"] == SECOND_NAME
    assert bytes(kept_rows[0]["content"]) == SECOND_BYTES
    assert before.json()["quota"] == after.json()["quota"] == 498
    counter, charges = _quota(account_id)
    assert counter["used"] == 2
    assert len(charges) == 2


def test_another_account_and_a_missing_origin_cannot_delete_the_bot(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_ready("library-bot-owner@example.com")
    stranger, _other = _open_ready("library-bot-stranger@example.com")

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        missing = client.delete(f"/api/bots/{bot['id']}")
    with _client(stranger) as client:
        foreign = client.delete(
            f"/api/bots/{bot['id']}",
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    assert missing.status_code == 403
    assert missing.json() == INVALID_ORIGIN
    assert foreign.status_code == 404
    assert foreign.json() == NOT_FOUND
    bot_id = uuid.UUID(bot["id"])
    assert [row for row in _rows(bots) if row["id"] == bot_id] != []
    stored = _library(bot["id"])
    assert len(stored) == 1
    assert bytes(stored[0]["content"]) == XLSX_BYTES
