import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    chats,
    messages,
    quota_counters,
    routines,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
CHAT_NOT_FOUND = {"code": "not_found", "message": "Чат не найден."}
MAIN_KEPT = {"code": "main_chat", "message": "Основной чат удалить нельзя."}
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


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_ready(email: str) -> str:
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
    return str(session_id)


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _bot(client: TestClient, scenario: str) -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": "описание", "scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _texts(bot_id: str, chat_id: uuid.UUID) -> list[str]:
    return [
        row["text"]
        for row in _rows(messages)
        if row["chat_id"] == chat_id
    ]


def test_extra_chat_starts_empty_with_the_same_scenario(database: None) -> None:
    cookie = _open_ready("extra-empty@example.com")
    with _client(cookie) as client:
        bot = _bot(client, "один сценарий")
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "история основного"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert posted.json()["messages"][1]["text"] == STUB
        before = client.get(f"/api/bots/{bot['id']}/chats")
        assert before.status_code == 200
        assert [row["kind"] for row in before.json()["chats"]] == ["main"]
        main_id = before.json()["chats"][0]["id"]
        main_updated = before.json()["chats"][0]["updated_at"]
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        extra = created.json()
        assert tuple(extra) == ("id", "kind", "created_at", "updated_at")
        assert extra["kind"] == "extra"
        assert extra["id"] != main_id
        assert extra["created_at"] == extra["updated_at"]
        empty = client.get(f"/api/bots/{bot['id']}/chats/{extra['id']}/messages")
        assert empty.status_code == 200
        assert empty.json() == {"messages": []}
        listed = client.get(f"/api/bots/{bot['id']}/chats")
        assert [row["kind"] for row in listed.json()["chats"]] == ["main", "extra"]
        assert listed.json()["chats"][0]["id"] == main_id
        assert listed.json()["chats"][0]["updated_at"] == main_updated
        assert listed.json()["chats"][1]["id"] == extra["id"]
        main_messages = client.get(f"/api/bots/{bot['id']}/messages")
        assert [row["text"] for row in main_messages.json()["messages"]] == [
            "история основного",
            STUB,
        ]
        card = client.get(f"/api/bots/{bot['id']}")
        assert card.json()["scenario"] == "один сценарий"
        second = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        assert second.status_code == 200
        assert second.json()["id"] != extra["id"]
        second_messages = client.get(
            f"/api/bots/{bot['id']}/chats/{second.json()['id']}/messages"
        )
        assert second_messages.json() == {"messages": []}
    stored = _rows(chats)
    bot_chats = [row for row in stored if str(row["bot_id"]) == bot["id"]]
    assert [row["kind"] for row in bot_chats].count("main") == 1
    extra_row = next(row for row in bot_chats if str(row["id"]) == extra["id"])
    assert _texts(bot["id"], extra_row["id"]) == []
    main_row = next(row for row in bot_chats if row["kind"] == "main")
    assert _texts(bot["id"], main_row["id"]) == ["история основного", STUB]


def test_extra_chat_rejects_a_foreign_bot_and_a_missing_origin(database: None) -> None:
    owner = _open_ready("extra-owner@example.com")
    other = _open_ready("extra-other@example.com")
    with _client(owner) as client:
        bot = _bot(client, "сценарий")
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        extra_id = created.json()["id"]
        rejected = client.post(f"/api/bots/{bot['id']}/chats")
        assert rejected.status_code == 403
        assert rejected.json() == INVALID_ORIGIN
    with _client(other) as client:
        missing = client.get(f"/api/bots/{bot['id']}/chats")
        assert missing.status_code == 404
        assert missing.json() == NOT_FOUND
        foreign = client.get(f"/api/bots/{bot['id']}/chats/{extra_id}/messages")
        assert foreign.status_code == 404
        assert foreign.json() == NOT_FOUND
    with _client(owner) as client:
        unknown = client.get(f"/api/bots/{bot['id']}/chats/{uuid.uuid4()}/messages")
        assert unknown.status_code == 404
        assert unknown.json() == CHAT_NOT_FOUND


def _insert_message(chat_id: uuid.UUID, text: str) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                messages.insert().values(
                    id=uuid.uuid4(),
                    chat_id=chat_id,
                    role="user",
                    text=text,
                    code=None,
                    updated_at=datetime(2026, 11, 16, 12, 1, tzinfo=timezone.utc),
                )
            )
    finally:
        engine.dispose()


def test_main_chat_stays_and_extra_chat_can_be_deleted(database: None) -> None:
    cookie = _open_ready("extra-delete@example.com")
    other = _open_ready("extra-delete-other@example.com")
    with _client(cookie) as client:
        bot = _bot(client, "сценарий")
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "основной"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        listed = client.get(f"/api/bots/{bot['id']}/chats")
        main_id = listed.json()["chats"][0]["id"]
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        extra_id = created.json()["id"]
        _insert_message(uuid.UUID(extra_id), "только здесь")
        refused = client.delete(
            f"/api/bots/{bot['id']}/chats/{main_id}",
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert refused.json() == MAIN_KEPT
        assert client.get(f"/api/bots/{bot['id']}/messages").json()["messages"][0]["text"] == "основной"
        missing_origin = client.delete(f"/api/bots/{bot['id']}/chats/{extra_id}")
        assert missing_origin.status_code == 403
        assert missing_origin.json() == INVALID_ORIGIN
        removed = client.delete(
            f"/api/bots/{bot['id']}/chats/{extra_id}",
            headers={"Origin": ORIGIN},
        )
        assert removed.status_code == 204
        assert removed.content == b""
        gone = client.get(f"/api/bots/{bot['id']}/chats/{extra_id}/messages")
        assert gone.status_code == 404
        assert gone.json() == CHAT_NOT_FOUND
        again = client.delete(
            f"/api/bots/{bot['id']}/chats/{extra_id}",
            headers={"Origin": ORIGIN},
        )
        assert again.status_code == 404
        assert again.json() == CHAT_NOT_FOUND
        remaining = client.get(f"/api/bots/{bot['id']}/chats")
        assert [row["id"] for row in remaining.json()["chats"]] == [main_id]
    with _client(other) as client:
        foreign = client.delete(
            f"/api/bots/{bot['id']}/chats/{main_id}",
            headers={"Origin": ORIGIN},
        )
        assert foreign.status_code == 404
        assert foreign.json() == NOT_FOUND
    stored = [row for row in _rows(chats) if str(row["bot_id"]) == bot["id"]]
    assert [str(row["id"]) for row in stored] == [main_id]
    assert _texts(bot["id"], uuid.UUID(main_id)) == ["основной", STUB]
    assert _texts(bot["id"], uuid.UUID(extra_id)) == []


def test_one_off_reply_stays_in_the_extra_chat(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie = _open_ready("extra-reply@example.com")
    seen: list[dict] = []

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str:
        seen.append(
            {
                "description": description,
                "scenario": scenario,
                "messages": messages,
                "desktop_available": desktop_available,
            }
        )
        return "ответ разового"

    monkeypatch.setattr("server.aggregator.complete", complete)
    with _client(cookie) as client:
        bot = _bot(client, "один сценарий")
        main = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "история основного"},
            headers={"Origin": ORIGIN},
        )
        assert main.status_code == 200
        created = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        extra_id = created.json()["id"]
        posted = client.post(
            f"/api/bots/{bot['id']}/chats/{extra_id}/messages",
            json={"text": "разовое поручение"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert [row["text"] for row in posted.json()["messages"]] == [
            "разовое поручение",
            "ответ разового",
        ]
        again = client.get(f"/api/bots/{bot['id']}/messages")
        assert [row["text"] for row in again.json()["messages"]] == [
            "история основного",
            "ответ разового",
        ]
        me = client.get("/api/me")
        assert me.json()["quota"] == 498
    assert seen[1] == {
        "description": "описание",
        "scenario": "один сценарий",
        "messages": [{"role": "user", "text": "разовое поручение"}],
        "desktop_available": False,
    }
    assert "история основного" not in str(seen[1])


def test_delete_extra_chat_leaves_the_bot_that_owns_the_library(
    database: None,
) -> None:
    cookie = _open_ready("extra-library@example.com")
    with _client(cookie) as client:
        bot = _bot(client, "сценарий библиотеки")
        routine = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [1], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        assert routine.status_code == 200
        routine_id = routine.json()["id"]
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
        assert posted.status_code == 200
        removed = client.delete(
            f"/api/bots/{bot['id']}/chats/{extra_id}",
            headers={"Origin": ORIGIN},
        )
        assert removed.status_code == 204
        card = client.get(f"/api/bots/{bot['id']}")
        assert card.status_code == 200
        assert card.json()["id"] == bot["id"]
        assert card.json()["scenario"] == "сценарий библиотеки"
        kept = client.get(f"/api/bots/{bot['id']}/routines")
        assert kept.status_code == 200
        assert [row["id"] for row in kept.json()] == [routine_id]
        remaining = client.get(f"/api/bots/{bot['id']}/chats")
        assert [row["kind"] for row in remaining.json()["chats"]] == ["main"]
        main_messages = client.get(f"/api/bots/{bot['id']}/messages")
        assert main_messages.json() == {"messages": []}
    bot_id = uuid.UUID(bot["id"])
    assert [row for row in _rows(bots) if row["id"] == bot_id][0]["scenario"] == (
        "сценарий библиотеки"
    )
    assert [row for row in _rows(routines) if row["bot_id"] == bot_id][0]["id"] == (
        uuid.UUID(routine_id)
    )
    assert _texts(bot["id"], uuid.UUID(extra_id)) == []
