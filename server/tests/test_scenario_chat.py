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
from server.config import development_tariff
from server.db import create_db_engine
from server.models import (
    accounts,
    bots,
    messages,
    quota_charges,
    quota_counters,
    subscription_periods,
)
from server.scenario_chat import proposed_scenario

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
OLD = "старый текст"
RULES = "Каждый день проверяй почту.\nПотом записывай итог."
REQUEST = f"Сохрани правила:\n{RULES}"
QUOTA_TEXT = "Квота на период закончилась."


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


def _bot(client: TestClient, scenario: str = OLD) -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": "описание", "scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _account_id(email: str) -> uuid.UUID:
    row = next(row for row in _rows(accounts) if row["email"] == email)
    return row["id"]


def _used(account_id: uuid.UUID) -> int:
    period_id = next(
        row["id"]
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    )
    counter = next(
        row for row in _rows(quota_counters) if row["period_id"] == period_id
    )
    return counter["used"]


def _charges(account_id: uuid.UUID) -> list[dict]:
    period_id = next(
        row["id"]
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    )
    return [row for row in _rows(quota_charges) if row["period_id"] == period_id]


def _set_used(account_id: uuid.UUID, used: int) -> None:
    period_id = next(
        row["id"]
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    )
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                quota_counters.update()
                .where(quota_counters.c.period_id == period_id)
                .values(used=used)
            )
    finally:
        engine.dispose()


def _scenario(bot_id: str) -> str:
    row = next(row for row in _rows(bots) if str(row["id"]) == bot_id)
    return row["scenario"]


def _refuse_aggregator(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_complete(**kwargs: object) -> str:
        raise AssertionError(kwargs)

    monkeypatch.setattr("server.aggregator.complete", fail_complete)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Сохрани правила: каждый день почта", "каждый день почта"),
        ("сохранить правила в сценарий — первая\nвторая", "первая\nвторая"),
        ("Сохрани задачу в сценарий: открывать почту", "открывать почту"),
        ("СОХРАНИ В СЦЕНАРИЙ текст целиком", "текст целиком"),
        ("Сохрани правила", None),
        ("Сохрани правила.", None),
        ("Потом сохрани правила: не это", None),
        ("Сохрани задачу на завтра", None),
        ("проверь почту", None),
    ],
)
def test_a_direct_request_carries_the_full_new_text(text: str, expected: str | None) -> None:
    assert proposed_scenario(text) == expected


def test_card_edit_still_saves_the_scenario(database: None) -> None:
    cookie = _open_ready("scenario-card@example.com")
    with _client(cookie) as client:
        bot = _bot(client)
        updated = client.patch(
            f"/api/bots/{bot['id']}",
            json={"scenario": "текст с карточки"},
            headers={"Origin": ORIGIN},
        )
        assert updated.status_code == 200
        assert updated.json()["scenario"] == "текст с карточки"
        listed = client.get(f"/api/bots/{bot['id']}/messages")
        assert listed.status_code == 200
        assert listed.json()["messages"] == []
    assert _scenario(bot["id"]) == "текст с карточки"


def test_save_rules_puts_the_full_text_in_the_chat(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    email = "scenario-show@example.com"
    cookie = _open_ready(email)
    account_id = _account_id(email)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": REQUEST},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        rows = posted.json()["messages"]
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert rows[0]["text"] == REQUEST
        assert rows[0]["code"] is None
        assert rows[1]["text"] == RULES
        assert rows[1]["code"] == "scenario_proposal"
        assert rows[1]["text"] != STUB
        card = client.get(f"/api/bots/{bot['id']}")
        assert card.json()["scenario"] == OLD
    assert _scenario(bot["id"]) == OLD
    assert _used(account_id) == 0
    assert _charges(account_id) == []
    stored = sorted(
        (row for row in _rows(messages) if row["text"] in {REQUEST, RULES}),
        key=lambda row: (row["updated_at"], str(row["id"])),
    )
    assert [row["text"] for row in stored] == [REQUEST, RULES]


def test_one_off_and_a_bare_request_do_not_replace_the_scenario(database: None) -> None:
    email = "scenario-one-off@example.com"
    cookie = _open_ready(email)
    account_id = _account_id(email)
    with _client(cookie) as client:
        bot = _bot(client)
        one_off = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "проверь почту"},
            headers={"Origin": ORIGIN},
        )
        assert one_off.status_code == 200
        assert [row["text"] for row in one_off.json()["messages"]] == [
            "проверь почту",
            STUB,
        ]
        bare = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "Сохрани правила"},
            headers={"Origin": ORIGIN},
        )
        assert bare.status_code == 200
        assert bare.json()["messages"][-1]["text"] == STUB
        assert bare.json()["messages"][-1]["code"] is None
        buried = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "Потом сохрани правила: не это"},
            headers={"Origin": ORIGIN},
        )
        assert buried.status_code == 200
        assert buried.json()["messages"][-1]["text"] == STUB
        card = client.get(f"/api/bots/{bot['id']}")
        assert card.json()["scenario"] == OLD
    assert _scenario(bot["id"]) == OLD
    assert _used(account_id) == 3
    assert len(_charges(account_id)) == 3


def test_exhausted_quota_still_shows_the_requested_text(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    email = "scenario-quota@example.com"
    cookie = _open_ready(email)
    account_id = _account_id(email)
    _set_used(account_id, development_tariff.model_requests)
    with _client(cookie) as client:
        bot = _bot(client)
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "Сохрани правила: почта утром"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        rows = posted.json()["messages"]
        assert rows[-1]["text"] == "почта утром"
        assert rows[-1]["code"] == "scenario_proposal"
        assert QUOTA_TEXT not in [row["text"] for row in rows]
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == OLD
    assert _used(account_id) == development_tariff.model_requests
    assert _charges(account_id) == []


NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
PROPOSAL_NOT_FOUND = {"code": "not_found", "message": "Сообщение не найдено."}
INVALID_CONFIRM = {"code": "invalid_request", "message": "Нужно подтверждение."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
SAVED = "Сценарий сохранён. Прежний текст заменён целиком."


def _proposal(client: TestClient, bot_id: str, text: str = REQUEST) -> dict:
    posted = client.post(
        f"/api/bots/{bot_id}/messages",
        json={"text": text},
        headers={"Origin": ORIGIN},
    )
    assert posted.status_code == 200
    proposal = posted.json()["messages"][-1]
    assert proposal["code"] == "scenario_proposal"
    return proposal


def test_confirm_button_is_in_the_chat() -> None:
    script = (Path(__file__).resolve().parents[2] / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    assert 'data-action="confirm-scenario"' in script
    assert ">Подтвердить</button>" in script
    assert "scenario_proposal" in script
    assert "scenario_confirmed" in script
    assert SAVED in script


def test_confirmation_replaces_the_scenario_and_silence_does_not(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    email = "scenario-confirm@example.com"
    cookie = _open_ready(email)
    other = _open_ready("scenario-confirm-other@example.com")
    with _client(cookie) as client:
        bot = _bot(client)
        proposal = _proposal(client, bot["id"])
        assert proposal["text"] == RULES
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == OLD
        silent = client.get(f"/api/bots/{bot['id']}/messages")
        assert silent.json()["messages"][1]["code"] == "scenario_proposal"
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == OLD
        missing_origin = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": True},
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json() == INVALID_ORIGIN
        refused = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": False},
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert refused.json() == INVALID_CONFIRM
        hidden = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": True, "password": "not-stored"},
            headers={"Origin": ORIGIN},
        )
        assert hidden.status_code == 400
        assert hidden.json() == INVALID_CONFIRM
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == OLD
        user_id = silent.json()["messages"][0]["id"]
        wrong = client.post(
            f"/api/bots/{bot['id']}/messages/{user_id}/scenario",
            json={"confirm": True},
            headers={"Origin": ORIGIN},
        )
        assert wrong.status_code == 404
        assert wrong.json() == PROPOSAL_NOT_FOUND
        confirmed = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": True},
            headers={"Origin": ORIGIN},
        )
        assert confirmed.status_code == 200
        rows = confirmed.json()["messages"]
        assert rows[0]["text"] == REQUEST
        assert rows[1]["text"] == RULES
        assert rows[1]["code"] == "scenario_confirmed"
        assert "Подтвердить" not in rows[1]["text"]
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == RULES
        edited = client.patch(
            f"/api/bots/{bot['id']}",
            json={"scenario": "текст с карточки"},
            headers={"Origin": ORIGIN},
        )
        assert edited.status_code == 200
        again = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": True},
            headers={"Origin": ORIGIN},
        )
        assert again.status_code == 200
        assert again.json()["messages"][1]["text"] == RULES
        assert client.get(f"/api/bots/{bot['id']}").json()["scenario"] == "текст с карточки"
    with _client(other) as client:
        foreign = client.post(
            f"/api/bots/{bot['id']}/messages/{proposal['id']}/scenario",
            json={"confirm": True},
            headers={"Origin": ORIGIN},
        )
        assert foreign.status_code == 404
        assert foreign.json() == NOT_FOUND
    stored = next(row for row in _rows(messages) if str(row["id"]) == proposal["id"])
    assert stored["text"] == RULES
    assert stored["code"] == "scenario_confirmed"
    assert _scenario(bot["id"]) == "текст с карточки"
    assert all("not-stored" not in row["text"] for row in _rows(messages))
