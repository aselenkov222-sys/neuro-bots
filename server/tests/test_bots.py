import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import create_account, create_session, format_utc
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    chats,
    sessions,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
COLORS = (
    "#c85a12",
    "#1f7a4c",
    "#5c6570",
    "#3d7a62",
    "#a34b12",
    "#7d8b86",
)
ICONS = ("mail", "chat", "table", "calendar", "box", "spark")
BOT_FIELDS = (
    "id",
    "name",
    "color",
    "icon",
    "description",
    "scenario",
    "updated_at",
    "chief",
)
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
INVALID_NAME = {
    "code": "invalid_name",
    "message": "Имя бота — от 1 до 80 символов.",
}
INVALID_DESCRIPTION = {
    "code": "invalid_description",
    "message": "Описание длиннее 280 символов.",
}
INVALID_SCENARIO = {
    "code": "invalid_scenario",
    "message": "Сценарий длиннее 16 000 символов.",
}
INVALID_COLOR = {
    "code": "invalid_avatar",
    "message": "Такого цвета в наборе нет.",
}
INVALID_ICON = {
    "code": "invalid_avatar",
    "message": "Такого значка в наборе нет.",
}
INVALID_OBJECT = {
    "code": "invalid_request",
    "message": "Нужен JSON-объект.",
}
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}


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
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(
                    email_confirmed_at=datetime(2026, 11, 16, tzinfo=timezone.utc)
                )
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _owned(table, account_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(table) if row["account_id"] == account_id]


def _chats_for(bot_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(chats) if row["bot_id"] == bot_id]


def _account_chats(account_id: uuid.UUID) -> list[dict]:
    ids = {row["id"] for row in _owned(bots, account_id)}
    return [row for row in _rows(chats) if row["bot_id"] in ids]


def _session(session_id: str) -> dict:
    matched = [row for row in _rows(sessions) if row["id"] == uuid.UUID(session_id)]
    assert len(matched) == 1
    return matched[0]


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _create(client: TestClient, payload: dict) -> object:
    return client.post("/api/bots", json=payload, headers={"Origin": ORIGIN})


def _patch(client: TestClient, bot_id: str, payload: dict) -> object:
    return client.patch(
        f"/api/bots/{bot_id}",
        json=payload,
        headers={"Origin": ORIGIN},
    )


def test_create_opens_one_main_chat_and_uses_the_default_avatar(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("create-bot@example.com")
    seen = _session(cookie)["last_request_at"]
    with _client(cookie) as client:
        created = _create(client, {"name": "  Искра  "})
        assert "set-cookie" not in created.headers
        assert created.status_code == 200
        body = created.json()
        assert tuple(body) == BOT_FIELDS
        assert body["name"] == "  Искра  "
        assert body["color"] == "#c85a12"
        assert body["icon"] == "spark"
        assert body["description"] == ""
        assert body["scenario"] == ""
        assert body["chief"] is False
        bot_id = uuid.UUID(body["id"])
        assert str(bot_id) == body["id"]
        stored = [row for row in _owned(bots, account_id) if row["id"] == bot_id]
        assert len(stored) == 1
        bot = stored[0]
        assert bot["account_id"] == account_id
        assert bot["name"] == "  Искра  "
        assert bot["color"] == "#c85a12"
        assert bot["icon"] == "spark"
        assert bot["description"] == ""
        assert bot["scenario"] == ""
        assert bot["chief"] is False
        assert body["updated_at"] == format_utc(bot["updated_at"])
        assert abs(
            (datetime.now(timezone.utc) - bot["updated_at"]).total_seconds()
        ) < 10
        opened_chats = _chats_for(bot_id)
        assert len(opened_chats) == 1
        assert opened_chats[0]["updated_at"] == bot["updated_at"]
        assert _session(cookie)["last_request_at"] == bot["updated_at"]
        assert _session(cookie)["last_request_at"] > seen
        assert _owned(subscription_periods, account_id) == []
        card = client.get(f"/api/bots/{body['id']}")
        listed = client.get("/api/bots")
    assert card.status_code == 200
    assert card.json() == body
    assert "set-cookie" not in card.headers
    assert listed.status_code == 200
    assert listed.json() == {"bots": [body]}
    assert _session(cookie)["last_request_at"] > bot["updated_at"]


def test_avatar_accepts_only_the_mockup_set(database: None) -> None:
    cookie, account_id = _open_confirmed("avatar-bot@example.com")
    with _client(cookie) as client:
        for color in COLORS:
            created = _create(client, {"name": color, "color": color})
            assert created.status_code == 200
            assert created.json()["color"] == color
            assert created.json()["icon"] == "spark"
        for icon in ICONS:
            created = _create(client, {"name": icon, "icon": icon})
            assert created.status_code == 200
            assert created.json()["icon"] == icon
            assert created.json()["color"] == "#c85a12"
        explicit = _create(
            client,
            {
                "name": "Мох",
                "color": "#1f7a4c",
                "icon": "mail",
                "model": "hidden",
                "updated_at": "1999-01-01T00:00:00+00:00",
            },
        )
        assert explicit.status_code == 200
        assert explicit.json()["color"] == "#1f7a4c"
        assert explicit.json()["icon"] == "mail"
        assert "model" not in explicit.json()
        assert explicit.json()["updated_at"] != "1999-01-01T00:00:00+00:00"
        before = len(_owned(bots, account_id))
        rejected_colors = ("#C85A12", "#000000", "c85a12", "#c85a12 ", "", None, 1)
        for color in rejected_colors:
            rejected = _create(client, {"name": "Нет", "color": color})
            assert rejected.status_code == 400
            assert rejected.json() == INVALID_COLOR
            assert "set-cookie" not in rejected.headers
        rejected_icons = ("shield", "lock", "send", "Spark", "SPARK", "", None, 1)
        for icon in rejected_icons:
            rejected = _create(client, {"name": "Нет", "icon": icon})
            assert rejected.status_code == 400
            assert rejected.json() == INVALID_ICON
        assert len(_owned(bots, account_id)) == before
        stored = _owned(bots, account_id)
        assert all("model" not in row for row in stored)
        assert all(row["updated_at"] != datetime(1999, 1, 1, tzinfo=timezone.utc) for row in stored)


def test_field_limits_reject_without_writing(database: None) -> None:
    cookie, account_id = _open_confirmed("limits-bot@example.com")
    name_80 = "я" * 80
    description_280 = "о" * 280
    scenario_16000 = "с" * 16000
    with _client(cookie) as client:
        edges = _create(
            client,
            {
                "name": f" {name_80[:78]} ",
                "description": description_280,
                "scenario": scenario_16000,
            },
        )
        assert edges.status_code == 200
        assert edges.json()["name"] == f" {name_80[:78]} "
        assert edges.json()["description"] == description_280
        assert edges.json()["scenario"] == scenario_16000
        blank = _create(client, {"name": " ", "description": "", "scenario": ""})
        assert blank.status_code == 200
        assert blank.json()["name"] == " "
        assert blank.json()["description"] == ""
        assert blank.json()["scenario"] == ""
        kept = len(_owned(bots, account_id))
        seen = _session(cookie)["last_request_at"]
        rejected = (
            ({"description": ""}, INVALID_NAME),
            ({"name": ""}, INVALID_NAME),
            ({"name": "я" * 81}, INVALID_NAME),
            ({"name": "  " + "я" * 79}, INVALID_NAME),
            ({"name": 1}, INVALID_NAME),
            ({"name": None}, INVALID_NAME),
            ({"name": "Бот", "description": "о" * 281}, INVALID_DESCRIPTION),
            ({"name": "Бот", "description": None}, INVALID_DESCRIPTION),
            ({"name": "Бот", "description": 1}, INVALID_DESCRIPTION),
            ({"name": "Бот", "scenario": "с" * 16001}, INVALID_SCENARIO),
            ({"name": "Бот", "scenario": None}, INVALID_SCENARIO),
            ({"name": "Бот", "scenario": 1}, INVALID_SCENARIO),
        )
        for payload, error in rejected:
            response = _create(client, payload)
            assert response.status_code == 400
            assert response.json() == error
            assert len(_owned(bots, account_id)) == kept
            assert len(_account_chats(account_id)) == kept
            assert _session(cookie)["last_request_at"] == seen
        objects = (None, [], "бот", 1, True)
        for value in objects:
            response = client.post(
                "/api/bots",
                json=value,
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 400
            assert response.json() == INVALID_OBJECT
        raw = client.post(
            "/api/bots",
            content=b"not-json",
            headers={"Origin": ORIGIN},
        )
        assert raw.status_code == 400
        assert raw.json() == INVALID_OBJECT
        assert len(_owned(bots, account_id)) == kept
        assert _session(cookie)["last_request_at"] == seen


def test_list_and_card_hide_another_accounts_bot(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_cookie, owner_id = _open_confirmed("owner-bot@example.com")
    other_cookie, other_id = _open_confirmed("other-bot@example.com")
    start = datetime(2026, 11, 17, 16, tzinfo=timezone.utc)
    ticks = {"n": 0}

    def frozen() -> datetime:
        ticks["n"] += 1
        return start + timedelta(minutes=ticks["n"])

    monkeypatch.setattr("server.bots.utc_now", frozen)
    with _client(owner_cookie) as owner, _client(other_cookie) as other:
        first = _create(owner, {"name": "Первый", "icon": "chat"})
        second = _create(owner, {"name": "Второй", "icon": "box"})
        foreign = _create(other, {"name": "Чужой", "icon": "mail"})
        assert first.status_code == second.status_code == foreign.status_code == 200
        listed = owner.get("/api/bots")
        assert listed.status_code == 200
        assert [bot["id"] for bot in listed.json()["bots"]] == [
            first.json()["id"],
            second.json()["id"],
        ]
        assert all(tuple(bot) == BOT_FIELDS for bot in listed.json()["bots"])
        card = owner.get(f"/api/bots/{first.json()['id']}")
        assert card.json() == first.json()
        missing = owner.get(f"/api/bots/{uuid.uuid4()}")
        hidden = owner.get(f"/api/bots/{foreign.json()['id']}")
        malformed = owner.get("/api/bots/not-a-uuid")
        assert missing.status_code == hidden.status_code == malformed.status_code == 404
        assert missing.json() == hidden.json() == malformed.json() == NOT_FOUND
        assert missing.text == hidden.text == malformed.text
        before = _session(owner_cookie)["last_request_at"]
        denied = owner.patch(
            f"/api/bots/{foreign.json()['id']}",
            json={"name": "Подмена"},
            headers={"Origin": ORIGIN},
        )
        denied_id = owner.patch(
            "/api/bots/not-a-uuid",
            json={"name": "Подмена"},
            headers={"Origin": ORIGIN},
        )
        assert denied.status_code == denied_id.status_code == 404
        assert denied.json() == denied_id.json() == NOT_FOUND
        assert _session(owner_cookie)["last_request_at"] == before
    foreign_row = next(row for row in _owned(bots, other_id) if row["name"] == "Чужой")
    assert foreign_row["account_id"] == other_id
    assert foreign_row["name"] == "Чужой"
    ids = [uuid.UUID(first.json()["id"]), uuid.UUID(second.json()["id"])]
    tied = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                bots.update().where(bots.c.id.in_(ids)).values(updated_at=tied)
            )
    finally:
        engine.dispose()
    with _client(owner_cookie) as owner:
        ordered = owner.get("/api/bots")
    assert [bot["id"] for bot in ordered.json()["bots"]] == [
        str(bot_id) for bot_id in sorted(ids)
    ]
    assert [bot["updated_at"] for bot in ordered.json()["bots"]] == [
        format_utc(tied),
        format_utc(tied),
    ]
    assert owner_id != other_id


def test_later_patch_keeps_the_second_text(database: None, monkeypatch) -> None:
    cookie, account_id = _open_confirmed("patch-bot@example.com")
    start = datetime(2026, 11, 17, 18, tzinfo=timezone.utc)
    ticks = {"n": 0}

    def frozen() -> datetime:
        ticks["n"] += 1
        return start + timedelta(minutes=ticks["n"])

    monkeypatch.setattr("server.bots.utc_now", frozen)
    with _client(cookie) as client:
        created = _create(
            client,
            {"name": "Черновик", "description": "старый", "scenario": "ход"},
        )
        assert created.status_code == 200
        bot_id = created.json()["id"]
        first = _patch(
            client,
            bot_id,
            {"name": "Первый", "description": "первый текст"},
        )
        assert first.status_code == 200
        assert first.json()["name"] == "Первый"
        assert first.json()["description"] == "первый текст"
        assert first.json()["scenario"] == "ход"
        assert first.json()["color"] == "#c85a12"
        assert first.json()["icon"] == "spark"
        seen = _session(cookie)["last_request_at"]
        rejected = _patch(client, bot_id, {"description": "о" * 281, "name": "Нет"})
        assert rejected.status_code == 400
        assert rejected.json() == INVALID_DESCRIPTION
        assert _session(cookie)["last_request_at"] == seen
        second = _patch(
            client,
            bot_id,
            {
                "description": "второй текст",
                "name": "Второй",
                "updated_at": "1999-01-01T00:00:00+00:00",
                "model": "hidden",
            },
        )
        assert second.status_code == 200
        assert "set-cookie" not in second.headers
        assert tuple(second.json()) == BOT_FIELDS
        assert second.json()["name"] == "Второй"
        assert second.json()["description"] == "второй текст"
        assert second.json()["scenario"] == "ход"
        assert second.json()["color"] == "#c85a12"
        assert second.json()["icon"] == "spark"
        assert "model" not in second.json()
        assert datetime.fromisoformat(second.json()["updated_at"]) > datetime.fromisoformat(
            first.json()["updated_at"]
        )
        assert second.json()["updated_at"] != "1999-01-01T00:00:00+00:00"
        assert _session(cookie)["last_request_at"] == datetime.fromisoformat(
            second.json()["updated_at"]
        )
        empty = _patch(client, bot_id, {})
        assert empty.status_code == 200
        assert empty.json()["name"] == "Второй"
        assert empty.json()["description"] == "второй текст"
        assert empty.json()["scenario"] == "ход"
        assert datetime.fromisoformat(empty.json()["updated_at"]) > datetime.fromisoformat(
            second.json()["updated_at"]
        )
        card = client.get(f"/api/bots/{bot_id}")
    assert card.json()["description"] == "второй текст"
    assert card.json()["name"] == "Второй"
    stored = [row for row in _owned(bots, account_id) if str(row["id"]) == bot_id]
    assert len(stored) == 1
    assert stored[0]["description"] == "второй текст"
    assert stored[0]["name"] == "Второй"
    assert stored[0]["scenario"] == "ход"
    assert format_utc(stored[0]["updated_at"]) == card.json()["updated_at"]
    assert len(_chats_for(uuid.UUID(bot_id))) == 1


def test_foreign_origin_does_not_write(database: None) -> None:
    cookie, account_id = _open_confirmed("origin-bot@example.com")
    with _client(cookie) as client:
        created = _create(client, {"name": "Свой"})
        assert created.status_code == 200
        bot_id = created.json()["id"]
        before = _owned(bots, account_id)
        seen = _session(cookie)["last_request_at"]
        missing = client.post("/api/bots", json={"name": "Чужой"})
        foreign = client.post(
            "/api/bots",
            content=b"not-json",
            headers={"Origin": "http://evil.example"},
        )
        patched = client.patch(
            f"/api/bots/{bot_id}",
            json={"name": "Подмена"},
            headers={"Origin": "http://evil.example"},
        )
    assert missing.status_code == foreign.status_code == patched.status_code == 403
    assert missing.json() == foreign.json() == patched.json() == INVALID_ORIGIN
    assert _owned(bots, account_id) == before
    assert _session(cookie)["last_request_at"] == seen
