import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    chats,
    messages,
    quota_counters,
    subscription_periods,
)
from server.routines import settle_due

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
INVALID_CHIEF = {"code": "invalid_request", "message": "Нужен признак главного."}
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
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


def _open_confirmed(email: str) -> tuple[str, uuid.UUID]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            confirmed_at = datetime(2026, 11, 16, tzinfo=timezone.utc)
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


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _create(client: TestClient, name: str) -> dict:
    response = client.post(
        "/api/bots",
        json={"name": name, "chief": True},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _stored_chief(bot_id: str) -> bool:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            value = connection.execute(
                select(bots.c.chief).where(bots.c.id == uuid.UUID(bot_id))
            ).scalar_one()
    finally:
        engine.dispose()
    return value is True


def test_the_card_assigns_one_chief_and_can_clear_it(database: None) -> None:
    cookie, _account_id = _open_confirmed("chief-one@example.com")
    other_cookie, _other = _open_confirmed("chief-other@example.com")
    with _client(cookie) as client:
        first = _create(client, "Почта")
        second = _create(client, "Таблица")
        assert first["chief"] is False
        assert second["chief"] is False
        assert _stored_chief(first["id"]) is False
        named = client.patch(
            f"/api/bots/{first['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert named.status_code == 200
        assert named.json()["chief"] is True
        assert named.json()["scenario"] == first["scenario"]
        assert named.json()["name"] == "Почта"
        assert _stored_chief(first["id"]) is True
        replaced = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert replaced.status_code == 200
        assert replaced.json()["chief"] is True
        listed = client.get("/api/bots")
        assert listed.status_code == 200
        by_id = {bot["id"]: bot["chief"] for bot in listed.json()["bots"]}
        assert by_id == {first["id"]: False, second["id"]: True}
        assert _stored_chief(first["id"]) is False
        assert _stored_chief(second["id"]) is True
        again = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert again.status_code == 200
        assert again.json()["chief"] is True
        cleared = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": False},
            headers={"Origin": ORIGIN},
        )
        assert cleared.status_code == 200
        assert cleared.json()["chief"] is False
        assert _stored_chief(second["id"]) is False
        kept = client.patch(
            f"/api/bots/{first['id']}",
            json={"name": "Почта 2"},
            headers={"Origin": ORIGIN},
        )
        assert kept.status_code == 200
        assert kept.json()["chief"] is False
        refused = client.patch(
            f"/api/bots/{first['id']}",
            json={"chief": "yes"},
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert refused.json() == INVALID_CHIEF
        missing = client.patch(
            f"/api/bots/{uuid.uuid4()}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert missing.status_code == 404
        assert missing.json() == NOT_FOUND
        foreign = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
        )
        assert foreign.status_code == 403
        assert foreign.json() == INVALID_ORIGIN
    with _client(other_cookie) as other:
        theirs = _create(other, "Свой")
        appointed = other.patch(
            f"/api/bots/{theirs['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert appointed.status_code == 200
        assert appointed.json()["chief"] is True
        stolen = other.patch(
            f"/api/bots/{second['id']}",
            json={"chief": False},
            headers={"Origin": ORIGIN},
        )
        assert stolen.status_code == 404
    assert _stored_chief(theirs["id"]) is True
    assert _stored_chief(second["id"]) is False


DUE = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
CLAIM_AT = DUE + timedelta(seconds=59)
SKIP_AT = DUE + timedelta(seconds=60)
SKIP_TEXT = "Рутина пропущена: рабочий десктоп не забрал запуск."
STUB_TEXT = "Сейчас я на заглушке агрегатора и не вызываю модель."
REPORT_CODE = "chief_report"


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _chat_messages(bot_id: str) -> list[dict]:
    parsed = uuid.UUID(bot_id)
    chat_ids = {row["id"] for row in _rows(chats) if row["bot_id"] == parsed}
    found = [row for row in _rows(messages) if row["chat_id"] in chat_ids]
    return sorted(found, key=lambda row: (row["updated_at"], str(row["id"])))


def _reports(bot_id: str) -> list[dict]:
    return [row for row in _chat_messages(bot_id) if row["code"] == REPORT_CODE]


def _scenario(bot_id: str) -> str:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            value = connection.execute(
                select(bots.c.scenario).where(bots.c.id == uuid.UUID(bot_id))
            ).scalar_one()
    finally:
        engine.dispose()
    return str(value)


def _settle(moment: datetime) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            settle_due(connection, moment)
    finally:
        engine.dispose()


def _desktop(client: TestClient) -> uuid.UUID:
    computer = uuid.uuid4()
    posted = client.post(
        "/api/desktops",
        json={"id": str(computer)},
        headers={"Origin": ORIGIN},
    )
    assert posted.status_code == 200
    pulsed = client.post(
        f"/api/desktops/{computer}/pulse",
        json={"timezone": "Europe/Moscow"},
        headers={"Origin": ORIGIN},
    )
    assert pulsed.status_code == 200
    return computer


def _routine(client: TestClient, bot_id: str, scenario: str) -> dict:
    patched = client.patch(
        f"/api/bots/{bot_id}",
        json={"scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert patched.status_code == 200
    created = client.post(
        f"/api/bots/{bot_id}/routines",
        json={"weekdays": [1, 3, 5], "time": "09:00"},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _appoint(client: TestClient, bot_id: str) -> None:
    appointed = client.patch(
        f"/api/bots/{bot_id}",
        json={"chief": True},
        headers={"Origin": ORIGIN},
    )
    assert appointed.status_code == 200
    assert appointed.json()["chief"] is True


def test_a_foreign_routine_reports_skip_finish_and_yes(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_confirmed("chief-report@example.com")
    calls: list[str] = []

    def spy(**kwargs: object) -> str:
        scenario = kwargs.get("scenario")
        calls.append(scenario if isinstance(scenario, str) else "")
        return STUB_TEXT

    monkeypatch.setattr("server.aggregator.complete", spy)
    with _client(cookie) as client:
        chief = _create(client, "Старший")
        worker = _create(client, "Почта")
        _appoint(client, chief["id"])
        digest = "Собери дайджест из отчётов."
        chief_scenario = client.patch(
            f"/api/bots/{chief['id']}",
            json={"scenario": digest},
            headers={"Origin": ORIGIN},
        )
        assert chief_scenario.status_code == 200
        computer = _desktop(client)
        _routine(client, worker["id"], "план")
    assert calls == []
    _settle(SKIP_AT)
    assert _scenario(chief["id"]) == digest
    assert {row["text"] for row in _reports(chief["id"])} == {
        "Рутина бота «Почта» пропущена."
    }
    assert _reports(chief["id"])[0]["role"] == "assistant"
    worker_chat = _chat_messages(worker["id"])
    assert [row["code"] for row in worker_chat] == ["routine_skipped"]
    assert worker_chat[0]["text"] == SKIP_TEXT
    assert calls == []

    with _client(cookie) as client:
        finished = _create(client, "Календарь")
        waiting = _create(client, "Оплата")
        _routine(client, finished["id"], "план")
        _routine(client, waiting["id"], "Отправить ответ.")
        monkeypatch.setattr("server.routines.utc_now", lambda: CLAIM_AT)
        claimed = client.post(
            f"/api/desktops/{computer}/routine-run",
            headers={"Origin": ORIGIN},
        )
        assert claimed.status_code == 200
        assert claimed.json()["bot_id"] == finished["id"]
        held = client.post(
            f"/api/desktops/{computer}/routine-run",
            headers={"Origin": ORIGIN},
        )
        assert held.status_code == 200
        assert held.json()["bot_id"] == waiting["id"]
    assert [row["text"] for row in _chat_messages(finished["id"])] == [STUB_TEXT]
    waiting_chat = _chat_messages(waiting["id"])
    assert any(row["code"] == "dangerous_action" for row in waiting_chat)
    assert {row["text"] for row in _reports(chief["id"])} == {
        "Рутина бота «Почта» пропущена.",
        "Рутина бота «Календарь» завершилась.",
        "Рутина бота «Оплата» ждёт «Да».",
    }
    assert all(row["code"] != "dangerous_action" for row in _reports(chief["id"]))
    assert calls == ["план"]
    assert _scenario(chief["id"]) == digest
    with _client(cookie) as client:
        checked = client.post(
            f"/api/bots/{worker['id']}/check",
            headers={"Origin": ORIGIN},
        )
        assert checked.status_code == 200
    assert {row["text"] for row in _reports(chief["id"])} == {
        "Рутина бота «Почта» пропущена.",
        "Рутина бота «Календарь» завершилась.",
        "Рутина бота «Оплата» ждёт «Да».",
    }


def test_no_chief_and_the_chiefs_own_routine_write_no_report(
    database: None,
) -> None:
    cookie, _account_id = _open_confirmed("chief-own@example.com")
    with _client(cookie) as client:
        alone = _create(client, "Один")
        _desktop(client)
        _routine(client, alone["id"], "план")
    _settle(SKIP_AT)
    assert _reports(alone["id"]) == []
    assert [row["code"] for row in _chat_messages(alone["id"])] == ["routine_skipped"]
    with _client(cookie) as client:
        _appoint(client, alone["id"])
        other = _create(client, "Чужой")
    _settle(SKIP_AT + timedelta(days=7))
    assert _reports(alone["id"]) == []
    assert _reports(other["id"]) == []
    own = _chat_messages(alone["id"])
    assert [row["code"] for row in own].count("routine_skipped") == 2
    assert all(row["code"] != REPORT_CODE for row in own)


def test_a_chief_assignment_starts_the_other_bots_turn(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_confirmed("chief-task@example.com")
    seen: list[str] = []

    def spy(**kwargs: object) -> str:
        scenario = kwargs.get("scenario")
        seen.append(scenario if isinstance(scenario, str) else "")
        return STUB_TEXT

    monkeypatch.setattr("server.aggregator.complete", spy)
    with _client(cookie) as client:
        chief = _create(client, "Старший")
        worker = _create(client, "Почта")
        worker_scenario = "Читать входящие."
        patched = client.patch(
            f"/api/bots/{worker['id']}",
            json={"scenario": worker_scenario},
            headers={"Origin": ORIGIN},
        )
        assert patched.status_code == 200
        extra = client.post(
            f"/api/bots/{worker['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        assert extra.status_code == 200
        extra_id = extra.json()["id"]
        refused = client.post(
            f"/api/bots/{chief['id']}/assignments",
            json={"bot_id": worker["id"], "text": "Проверь ящик."},
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert refused.json() == {
            "code": "not_chief",
            "message": "Этот бот не главный.",
        }
        assert seen == []
        _appoint(client, chief["id"])
        assigned = client.post(
            f"/api/bots/{chief['id']}/assignments",
            json={"bot_id": worker["id"], "text": "Проверь ящик."},
            headers={"Origin": ORIGIN},
        )
        assert assigned.status_code == 200
        body = assigned.json()["messages"]
        assert [row["role"] for row in body] == ["user", "assistant"]
        assert body[0]["text"] == "Проверь ящик."
        assert body[0]["code"] == "chief_assignment"
        assert body[1]["text"] == STUB_TEXT
        assert _scenario(worker["id"]) == worker_scenario
        assert _scenario(chief["id"]) == ""
        assert _chat_messages(chief["id"]) == []
        extra_messages = client.get(
            f"/api/bots/{worker['id']}/chats/{extra_id}/messages"
        )
        assert extra_messages.status_code == 200
        assert extra_messages.json()["messages"] == []
        self_task = client.post(
            f"/api/bots/{chief['id']}/assignments",
            json={"bot_id": chief["id"], "text": "Себе."},
            headers={"Origin": ORIGIN},
        )
        assert self_task.status_code == 400
        assert self_task.json()["message"] == "Поручение уходит другому боту."
        assert _chat_messages(chief["id"]) == []
    assert seen == [worker_scenario]


def test_a_report_is_a_chat_message_and_not_a_digest(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id = _open_confirmed("chief-digest@example.com")
    seen: list[dict[str, object]] = []

    def spy(**kwargs: object) -> str:
        seen.append(kwargs)
        return STUB_TEXT

    monkeypatch.setattr("server.aggregator.complete", spy)
    digest = "Собери дайджест из отчётов."
    with _client(cookie) as client:
        chief = _create(client, "Старший")
        worker = _create(client, "Почта")
        _appoint(client, chief["id"])
        patched = client.patch(
            f"/api/bots/{chief['id']}",
            json={"scenario": digest},
            headers={"Origin": ORIGIN},
        )
        assert patched.status_code == 200
        _desktop(client)
        _routine(client, worker["id"], "план")
    _settle(SKIP_AT)
    assert seen == []
    before = _chat_messages(chief["id"])
    assert [row["code"] for row in before] == [REPORT_CODE]
    assert _scenario(chief["id"]) == digest
    assert _scenario(worker["id"]) == "план"
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            names = connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            ).scalars().all()
    finally:
        engine.dispose()
    assert all("digest" not in name for name in names)
    with _client(cookie) as client:
        spoken = client.post(
            f"/api/bots/{chief['id']}/messages",
            json={"text": "Что в отчётах?"},
            headers={"Origin": ORIGIN},
        )
        assert spoken.status_code == 200
    assert len(seen) == 1
    assert seen[0]["scenario"] == digest
    handed = seen[0]["messages"]
    assert isinstance(handed, list)
    assert [item["text"] for item in handed] == [
        "Рутина бота «Почта» пропущена.",
        "Что в отчётах?",
    ]
    after = _chat_messages(chief["id"])
    assert [row["text"] for row in after] == [
        "Рутина бота «Почта» пропущена.",
        "Что в отчётах?",
        STUB_TEXT,
    ]
    assert [row["code"] for row in after] == [REPORT_CODE, None, None]
    assert _scenario(chief["id"]) == digest
    assert _scenario(worker["id"]) == "план"
    assert _chat_messages(worker["id"])[0]["code"] == "routine_skipped"


def test_cabinet_assigns_the_chief_on_the_bot_card() -> None:
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    card = script.split("function botScreen(", 1)[1].split("function accessSite(", 1)[0]
    bots = script.split("function botCard(", 1)[1].split("function botsScreen(", 1)[0]
    chat = script.split("function chatScreen(", 1)[1].split("function render(", 1)[0]
    assert 'id="main-toggle"' in card
    assert "Главный" in card
    assert "bot.chief === true" in card
    assert 'class="chief">Главный' in bots
    assert 'class="chief">Главный' in chat
    assert 'id="main-toggle"' in script
    assert "patchBot({ chief: target.checked })" in script
    assert ".switch" in styles
    assert ".chief" in styles
