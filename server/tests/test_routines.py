import subprocess
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Integer,
    Text,
    Time,
    Uuid,
    select,
    text,
)

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.config import development_tariff
from server.db import create_db_engine, metadata
from server.models import (
    accounts,
    bots,
    chats,
    desktops,
    messages,
    quota_charges,
    quota_counters,
    routine_runs,
    routines,
    sessions,
    subscription_periods,
)
from server.routines import (
    appointed_utc,
    routine_may_start,
    settle_due,
    starts_when_due,
    working_desktop_zone,
    working_routines_stopped,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
ROUTINE_COLUMNS = (
    "id",
    "bot_id",
    "weekdays",
    "time_of_day",
    "paused",
    "created_at",
)
ROUTINE_FIELDS = ("id", "weekdays", "time", "paused")
INVALID_PAUSE = {"code": "invalid_request", "message": "Нужен признак паузы."}
INVALID_ROUTINE = {
    "code": "invalid_routine",
    "message": "Нужны дни недели и одно время.",
}
SCENARIO_REQUIRED = {
    "code": "scenario_required",
    "message": "Без сценария рутину создать нельзя.",
}
ROUTINE_LIMIT = {
    "code": "routine_limit",
    "message": "Лимит рутин исчерпан.",
}
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
ROUTINE_NOT_FOUND = {"code": "not_found", "message": "Рутина не найдена."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
EMAIL_UNCONFIRMED = {
    "code": "email_unconfirmed",
    "message": "Почта ещё не подтверждена.",
}
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}


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


def _open_account(email: str, *, confirmed: bool) -> tuple[str, uuid.UUID]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            if confirmed:
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


def _session(session_id: str) -> dict:
    matched = [row for row in _rows(sessions) if row["id"] == uuid.UUID(session_id)]
    assert len(matched) == 1
    return matched[0]


def _chat_messages(bot_id: uuid.UUID) -> list[dict]:
    chat_ids = {row["id"] for row in _rows(chats) if row["bot_id"] == bot_id}
    return [row for row in _rows(messages) if row["chat_id"] in chat_ids]


def _account_routines(account_id: uuid.UUID) -> list[dict]:
    bot_ids = {
        row["id"] for row in _rows(bots) if row["account_id"] == account_id
    }
    return [row for row in _rows(routines) if row["bot_id"] in bot_ids]


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _create_bot(client: TestClient, name: str, scenario: str) -> dict:
    response = client.post(
        "/api/bots",
        json={"name": name, "scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _pause(client: TestClient, bot_id: str, routine_id: str, paused: object) -> object:
    return client.patch(
        f"/api/bots/{bot_id}/routines/{routine_id}",
        json={"paused": paused},
        headers={"Origin": ORIGIN},
    )


def _create_routine(client: TestClient, bot_id: str, payload: dict) -> object:
    return client.post(
        f"/api/bots/{bot_id}/routines",
        json=payload,
        headers={"Origin": ORIGIN},
    )


def _insert_desktop(
    account_id: uuid.UUID, *, working: bool, zone: str | None
) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                desktops.insert().values(
                    id=uuid.uuid4(),
                    account_id=account_id,
                    computer_id=uuid.uuid4(),
                    working=working,
                    created_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
                    timezone=zone,
                )
            )
    finally:
        engine.dispose()


def _zone(account_id: uuid.UUID) -> str | None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            return working_desktop_zone(connection, account_id)
    finally:
        engine.dispose()


def test_routine_model_and_table_have_schedule_columns_only(database: None) -> None:
    assert routines.metadata is metadata
    assert tuple(routines.columns.keys()) == ROUTINE_COLUMNS
    assert [column.name for column in routines.primary_key] == ["id"]
    assert isinstance(routines.c.id.type, Uuid)
    assert routines.c.id.nullable is False
    assert isinstance(routines.c.bot_id.type, Uuid)
    assert routines.c.bot_id.nullable is False
    foreign_keys = list(routines.c.bot_id.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].column.table.name == "bots"
    assert foreign_keys[0].column.name == "id"
    assert isinstance(routines.c.weekdays.type, ARRAY)
    assert isinstance(routines.c.weekdays.type.item_type, Integer)
    assert routines.c.weekdays.nullable is False
    assert isinstance(routines.c.time_of_day.type, Time)
    assert routines.c.time_of_day.type.timezone is False
    assert routines.c.time_of_day.nullable is False
    for column in routines.columns:
        assert not isinstance(column.type, Text)
        assert column.name not in {"scenario", "text", "title"}
    assert isinstance(routines.c.paused.type, Boolean)
    assert routines.c.paused.nullable is False
    assert routines.c.paused.server_default.arg.text == "false"
    assert isinstance(routines.c.created_at.type, DateTime)
    assert routines.c.created_at.type.timezone is True
    assert routines.c.created_at.nullable is False
    assert routines.c.created_at.server_default.arg.text == "now()"

    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            columns = (
                connection.execute(
                    text(
                        """
                        SELECT
                            a.attname AS column_name,
                            a.attnotnull AS not_null,
                            pg_catalog.format_type(a.atttypid, a.atttypmod)
                                AS format_type
                        FROM pg_catalog.pg_attribute AS a
                        JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relname = 'routines'
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                        ORDER BY a.attnum
                        """
                    )
                )
                .mappings()
                .all()
            )
            constraints = connection.execute(
                text(
                    """
                    SELECT c.contype AS constraint_type,
                           pg_get_constraintdef(c.oid) AS definition
                    FROM pg_catalog.pg_constraint AS c
                    JOIN pg_catalog.pg_class AS rel ON rel.oid = c.conrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = rel.relnamespace
                    WHERE n.nspname = 'public'
                      AND rel.relname = 'routines'
                    """
                )
            ).all()
    finally:
        engine.dispose()
    assert tuple(row["column_name"] for row in columns) == ROUTINE_COLUMNS
    assert [row["format_type"] for row in columns] == [
        "uuid",
        "uuid",
        "integer[]",
        "time without time zone",
        "boolean",
        "timestamp with time zone",
    ]
    assert all(row["not_null"] is True for row in columns)
    assert set(constraints) == {
        ("p", "PRIMARY KEY (id)"),
        ("f", "FOREIGN KEY (bot_id) REFERENCES bots(id)"),
    }


def test_create_routine_for_a_bot_with_a_scenario(database: None) -> None:
    cookie, account_id = _open_account("create-routine@example.com", confirmed=True)
    seen = _session(cookie)["last_request_at"]
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "  утренний план  ")
        created = _create_routine(
            client,
            bot["id"],
            {
                "weekdays": [5, 1, 3],
                "time": "09:00",
                "text": "секрет",
                "scenario": "другой",
                "title": "утро",
            },
        )
        assert "set-cookie" not in created.headers
        assert created.status_code == 200
        body = created.json()
        assert tuple(body) == ROUTINE_FIELDS
        assert body["weekdays"] == [1, 3, 5]
        assert body["time"] == "09:00"
        routine_id = uuid.UUID(body["id"])
        assert str(routine_id) == body["id"]
        edges = (
            ({"weekdays": [7], "time": "23:59"}, [7], "23:59"),
            ({"weekdays": [1], "time": "00:00"}, [1], "00:00"),
        )
        for payload, weekdays, clock in edges:
            response = _create_routine(client, bot["id"], payload)
            assert response.status_code == 200
            assert response.json()["weekdays"] == weekdays
            assert response.json()["time"] == clock
        card = client.get(f"/api/bots/{bot['id']}")
    assert card.json()["scenario"] == "  утренний план  "
    stored = _account_routines(account_id)
    assert len(stored) == 3
    first = next(row for row in stored if row["id"] == routine_id)
    assert set(first) == set(ROUTINE_COLUMNS)
    assert first["bot_id"] == uuid.UUID(bot["id"])
    assert list(first["weekdays"]) == [1, 3, 5]
    assert first["time_of_day"] == time(9, 0)
    assert "text" not in first
    assert _session(cookie)["last_request_at"] > seen


def test_empty_or_whitespace_scenario_creates_no_routine(database: None) -> None:
    cookie, account_id = _open_account("blank-routine@example.com", confirmed=True)
    with _client(cookie) as client:
        blank_bots = (
            _create_bot(client, "Пустой", ""),
            _create_bot(client, "Пробел", " "),
            _create_bot(client, "Пробелы", " \n\t "),
        )
        seen = _session(cookie)["last_request_at"]
        for bot in blank_bots:
            rejected = _create_routine(
                client,
                bot["id"],
                {"weekdays": [1, 3, 5], "time": "09:00"},
            )
            assert rejected.status_code == 400
            assert rejected.json() == SCENARIO_REQUIRED
            assert _session(cookie)["last_request_at"] == seen
    assert _account_routines(account_id) == []


def test_invalid_day_duplicate_day_and_bad_time_are_rejected(
    database: None,
) -> None:
    cookie, account_id = _open_account("invalid-routine@example.com", confirmed=True)
    with _client(cookie) as client:
        bot = _create_bot(client, "Сценарий", "план")
        seen = _session(cookie)["last_request_at"]
        payloads = (
            {"weekdays": [0], "time": "09:00"},
            {"weekdays": [8], "time": "09:00"},
            {"weekdays": [1, 1], "time": "09:00"},
            {"weekdays": [3, 1, 3], "time": "09:00"},
            {"weekdays": [1], "time": "24:00"},
            {"weekdays": [1], "time": "09:60"},
            {"weekdays": [1], "time": "9:00"},
            {"weekdays": [1], "time": "09:00:00"},
            {"weekdays": [], "time": "09:00"},
            {"weekdays": [1]},
            {"time": "09:00"},
            {"weekdays": "135", "time": "09:00"},
            {"weekdays": [True], "time": "09:00"},
            {"weekdays": [1.5], "time": "09:00"},
        )
        for payload in payloads:
            rejected = _create_routine(client, bot["id"], payload)
            assert rejected.status_code == 400
            assert rejected.json() == INVALID_ROUTINE
            assert _account_routines(account_id) == []
            assert _session(cookie)["last_request_at"] == seen
        for value in (None, [], "утро", 1, True):
            rejected = client.post(
                f"/api/bots/{bot['id']}/routines",
                json=value,
                headers={"Origin": ORIGIN},
            )
            assert rejected.status_code == 400
            assert rejected.json() == INVALID_ROUTINE
        raw = client.post(
            f"/api/bots/{bot['id']}/routines",
            content=b"not-json",
            headers={"Origin": ORIGIN},
        )
        assert raw.status_code == 400
        assert raw.json() == INVALID_ROUTINE
        assert _account_routines(account_id) == []
        assert _session(cookie)["last_request_at"] == seen


def test_sixth_routine_is_rejected_until_one_is_deleted(database: None) -> None:
    assert development_tariff.routines == 5
    cookie, account_id = _open_account("limit-routine@example.com", confirmed=True)
    with _client(cookie) as client:
        first = _create_bot(client, "Первый", "сценарий")
        second = _create_bot(client, "Второй", "другой сценарий")
        created_ids = []
        for index in range(development_tariff.routines):
            created = _create_routine(
                client,
                first["id"],
                {"weekdays": [index + 1], "time": f"09:{index:02d}"},
            )
            assert created.status_code == 200
            created_ids.append(created.json()["id"])
        assert len(_account_routines(account_id)) == 5
        blocked = _create_routine(
            client,
            second["id"],
            {"weekdays": [6], "time": "10:00"},
        )
        assert blocked.status_code == 400
        assert blocked.json() == ROUTINE_LIMIT
        assert len(_account_routines(account_id)) == 5
        assert [
            row for row in _account_routines(account_id) if str(row["bot_id"]) == second["id"]
        ] == []
        removed = client.delete(
            f"/api/bots/{first['id']}/routines/{created_ids[0]}",
            headers={"Origin": ORIGIN},
        )
        assert removed.status_code == 200
        assert removed.json() == {}
        assert "set-cookie" not in removed.headers
        assert len(_account_routines(account_id)) == 4
        gone = client.delete(
            f"/api/bots/{first['id']}/routines/{created_ids[0]}",
            headers={"Origin": ORIGIN},
        )
        assert gone.status_code == 404
        assert gone.json() == ROUTINE_NOT_FOUND
        again = _create_routine(
            client,
            second["id"],
            {"weekdays": [6], "time": "10:00"},
        )
        assert again.status_code == 200
        assert again.json()["weekdays"] == [6]
        assert again.json()["time"] == "10:00"
    stored = _account_routines(account_id)
    assert len(stored) == 5
    assert created_ids[0] not in {str(row["id"]) for row in stored}
    assert any(row["bot_id"] == uuid.UUID(second["id"]) for row in stored)
    assert sum(row["bot_id"] == uuid.UUID(first["id"]) for row in stored) == 4


def test_missing_and_foreign_routines_are_not_deleted(database: None) -> None:
    owner_cookie, owner_id = _open_account("owner-routine@example.com", confirmed=True)
    other_cookie, other_id = _open_account("other-routine@example.com", confirmed=True)
    with _client(owner_cookie) as owner, _client(other_cookie) as other:
        bot = _create_bot(owner, "Свой", "сценарий")
        created = _create_routine(
            owner,
            bot["id"],
            {"weekdays": [2, 4], "time": "12:30"},
        )
        assert created.status_code == 200
        routine_id = created.json()["id"]
        missing_bot = _create_routine(
            owner,
            str(uuid.uuid4()),
            {"weekdays": [1], "time": "09:00"},
        )
        malformed_bot = _create_routine(
            owner,
            "not-a-uuid",
            {"weekdays": [1], "time": "09:00"},
        )
        assert missing_bot.status_code == malformed_bot.status_code == 404
        assert missing_bot.json() == malformed_bot.json() == NOT_FOUND
        hidden = other.delete(
            f"/api/bots/{bot['id']}/routines/{routine_id}",
            headers={"Origin": ORIGIN},
        )
        assert hidden.status_code == 404
        assert hidden.json() == NOT_FOUND
        other_bot = _create_bot(other, "Чужой", "свой сценарий")
        stolen = other.delete(
            f"/api/bots/{other_bot['id']}/routines/{routine_id}",
            headers={"Origin": ORIGIN},
        )
        missing = owner.delete(
            f"/api/bots/{bot['id']}/routines/{uuid.uuid4()}",
            headers={"Origin": ORIGIN},
        )
        malformed = owner.delete(
            f"/api/bots/{bot['id']}/routines/not-a-uuid",
            headers={"Origin": ORIGIN},
        )
        assert stolen.status_code == missing.status_code == malformed.status_code == 404
        assert stolen.json() == missing.json() == malformed.json() == ROUTINE_NOT_FOUND
    stored = _account_routines(owner_id)
    assert len(stored) == 1
    assert str(stored[0]["id"]) == routine_id
    assert stored[0]["weekdays"] == [2, 4]
    assert stored[0]["time_of_day"] == time(12, 30)
    assert _account_routines(other_id) == []


def test_origin_and_session_reject_routine_changes(database: None) -> None:
    cookie, account_id = _open_account("origin-routine@example.com", confirmed=True)
    unconfirmed, _ = _open_account(
        "unconfirmed-routine@example.com", confirmed=False
    )
    with _client(cookie) as client:
        bot = _create_bot(client, "Свой", "сценарий")
        created = _create_routine(
            client,
            bot["id"],
            {"weekdays": [1], "time": "09:00"},
        )
        assert created.status_code == 200
        routine_id = created.json()["id"]
        seen = _session(cookie)["last_request_at"]
        missing_origin = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [2], "time": "11:00"},
        )
        foreign = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [3], "time": "11:00"},
            headers={"Origin": "http://evil.example"},
        )
        foreign_delete = client.delete(
            f"/api/bots/{bot['id']}/routines/{routine_id}",
            headers={"Origin": "http://evil.example"},
        )
        assert (
            missing_origin.status_code
            == foreign.status_code
            == foreign_delete.status_code
            == 403
        )
        assert missing_origin.json() == foreign.json() == foreign_delete.json() == (
            INVALID_ORIGIN
        )
        assert _session(cookie)["last_request_at"] == seen
    with _client(unconfirmed) as client:
        rejected = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [4], "time": "11:00"},
            headers={"Origin": ORIGIN},
        )
        assert rejected.status_code == 403
        assert rejected.json() == EMAIL_UNCONFIRMED
    with TestClient(app) as anonymous:
        denied = anonymous.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [5], "time": "11:00"},
            headers={"Origin": ORIGIN},
        )
    assert denied.status_code == 401
    assert denied.json() == UNAUTHENTICATED
    stored = _account_routines(account_id)
    assert len(stored) == 1
    assert str(stored[0]["id"]) == routine_id


def test_appointed_utc_uses_the_desktop_zone() -> None:
    clock = time(9, 0)
    local_date = date(2026, 11, 18)
    utc_nine = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
    moscow = appointed_utc(clock, "Europe/Moscow", local_date)
    new_york = appointed_utc(clock, "America/New_York", local_date)
    assert moscow == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    assert new_york == datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
    assert moscow.tzinfo is timezone.utc
    assert new_york.tzinfo is timezone.utc
    assert moscow != utc_nine
    assert new_york != utc_nine
    for zone_name in ("Not/AZone", "Europe/Moskow", "UTC+3", ""):
        with pytest.raises(ValueError):
            appointed_utc(clock, zone_name, local_date)


def test_saved_clock_stays_local_in_the_working_desktop_zone(
    database: None,
) -> None:
    _, absent_id = _open_account("absent-zone@example.com", confirmed=True)
    _, null_id = _open_account("null-zone@example.com", confirmed=True)
    _, idle_id = _open_account("idle-zone@example.com", confirmed=True)
    cookie, account_id = _open_account("moscow-zone@example.com", confirmed=True)
    _insert_desktop(null_id, working=True, zone=None)
    _insert_desktop(idle_id, working=False, zone="Europe/Moscow")
    _insert_desktop(account_id, working=True, zone="Europe/Moscow")
    assert _zone(absent_id) is None
    assert _zone(null_id) is None
    assert _zone(idle_id) is None
    assert _zone(account_id) == "Europe/Moscow"
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "план")
        created = _create_routine(
            client,
            bot["id"],
            {"weekdays": [3], "time": "09:00"},
        )
        assert created.status_code == 200
        assert tuple(created.json()) == ROUTINE_FIELDS
        assert created.json()["time"] == "09:00"
        routine_id = uuid.UUID(created.json()["id"])
    stored = _account_routines(account_id)
    assert len(stored) == 1
    row = stored[0]
    assert set(row) == set(ROUTINE_COLUMNS)
    assert row["time_of_day"] == time(9, 0)
    assert row["time_of_day"].tzinfo is None
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            shown = connection.execute(
                text("SELECT time_of_day::text FROM routines WHERE id = :id"),
                {"id": routine_id},
            ).scalar_one()
            stored_type = connection.execute(
                text(
                    "SELECT pg_typeof(time_of_day)::text"
                    " FROM routines WHERE id = :id"
                ),
                {"id": routine_id},
            ).scalar_one()
    finally:
        engine.dispose()
    assert shown == "09:00:00"
    assert stored_type == "time without time zone"
    appointed = appointed_utc(row["time_of_day"], _zone(account_id), date(2026, 11, 18))
    assert appointed == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    assert appointed != datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def test_pause_blocks_a_start_and_writes_no_chat_message(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_complete(*args: object, **kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", fail_complete)
    cookie, account_id = _open_account("pause-routine@example.com", confirmed=True)
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "план")
        created = _create_routine(
            client,
            bot["id"],
            {"weekdays": [1, 3], "time": "09:00"},
        )
        assert created.status_code == 200
        assert tuple(created.json()) == ROUTINE_FIELDS
        assert created.json()["paused"] is False
        routine_id = created.json()["id"]
        bot_id = uuid.UUID(bot["id"])
        created_row = _account_routines(account_id)[0]
        assert created_row["paused"] is False
        assert starts_when_due(created_row) is True
        assert len([row for row in _rows(chats) if row["bot_id"] == bot_id]) == 1
        assert _chat_messages(bot_id) == []
        for payload in (1, "true", "false", None):
            rejected = _pause(client, bot["id"], routine_id, payload)
            assert rejected.status_code == 400
            assert rejected.json() == INVALID_PAUSE
        missing_flag = client.patch(
            f"/api/bots/{bot['id']}/routines/{routine_id}",
            json={},
            headers={"Origin": ORIGIN},
        )
        foreign_shape = client.patch(
            f"/api/bots/{bot['id']}/routines/{routine_id}",
            json=[],
            headers={"Origin": ORIGIN},
        )
        raw = client.patch(
            f"/api/bots/{bot['id']}/routines/{routine_id}",
            content=b"not-json",
            headers={"Origin": ORIGIN},
        )
        assert (
            missing_flag.status_code
            == foreign_shape.status_code
            == raw.status_code
            == 400
        )
        assert missing_flag.json() == foreign_shape.json() == raw.json() == INVALID_PAUSE
        missing_bot = client.patch(
            f"/api/bots/{uuid.uuid4()}/routines/{routine_id}",
            json={"paused": True},
            headers={"Origin": ORIGIN},
        )
        missing_routine = client.patch(
            f"/api/bots/{bot['id']}/routines/{uuid.uuid4()}",
            json={"paused": True},
            headers={"Origin": ORIGIN},
        )
        assert missing_bot.status_code == 404
        assert missing_bot.json() == NOT_FOUND
        assert missing_routine.status_code == 404
        assert missing_routine.json() == ROUTINE_NOT_FOUND
        assert _account_routines(account_id)[0]["paused"] is False
        assert _chat_messages(bot_id) == []
        paused = _pause(client, bot["id"], routine_id, True)
        assert paused.status_code == 200
        assert tuple(paused.json()) == ROUTINE_FIELDS
        assert paused.json()["paused"] is True
        assert paused.json()["weekdays"] == [1, 3]
        assert paused.json()["time"] == "09:00"
        held = _account_routines(account_id)[0]
        assert held["paused"] is True
        assert starts_when_due(held) is False
        assert held["time_of_day"] == time(9, 0)
        assert _chat_messages(bot_id) == []
        resumed = _pause(client, bot["id"], routine_id, False)
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False
        assert resumed.json()["time"] == "09:00"
        assert resumed.json()["weekdays"] == [1, 3]
        released = _account_routines(account_id)[0]
        assert released["paused"] is False
        assert starts_when_due(released) is True
        assert released["time_of_day"] == time(9, 0)
        assert _chat_messages(bot_id) == []


def test_list_and_patch_weekdays_and_time(database: None) -> None:
    cookie, account_id = _open_account("list-routine@example.com", confirmed=True)
    other, _ = _open_account("list-routine-other@example.com", confirmed=True)
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "план")
        empty = client.get(f"/api/bots/{bot['id']}/routines")
        assert empty.status_code == 200
        assert empty.json() == []
        first = _create_routine(
            client, bot["id"], {"weekdays": [1, 3], "time": "09:00"}
        )
        second = _create_routine(
            client, bot["id"], {"weekdays": [5], "time": "18:00"}
        )
        assert first.status_code == second.status_code == 200
        listed = client.get(f"/api/bots/{bot['id']}/routines")
        assert listed.status_code == 200
        rows = listed.json()
        assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)
        assert rows == sorted(
            [first.json(), second.json()], key=lambda row: row["id"]
        )
        assert all(tuple(row) == ROUTINE_FIELDS for row in rows)
        chosen = rows[0]
        patched = client.patch(
            f"/api/bots/{bot['id']}/routines/{chosen['id']}",
            json={"weekdays": [4, 2], "time": "10:30"},
            headers={"Origin": ORIGIN},
        )
        assert patched.status_code == 200
        assert patched.json()["weekdays"] == [2, 4]
        assert patched.json()["time"] == "10:30"
        assert patched.json()["paused"] is False
        assert patched.json()["id"] == chosen["id"]
        stored = [
            row
            for row in _account_routines(account_id)
            if str(row["id"]) == chosen["id"]
        ]
        assert len(stored) == 1
        assert list(stored[0]["weekdays"]) == [2, 4]
        assert stored[0]["time_of_day"] == time(10, 30)
        assert stored[0]["paused"] is False
        rejected = client.patch(
            f"/api/bots/{bot['id']}/routines/{chosen['id']}",
            json={"weekdays": [1], "time": "25:00"},
            headers={"Origin": ORIGIN},
        )
        assert rejected.status_code == 400
        assert rejected.json() == INVALID_ROUTINE
        cleared = client.patch(
            f"/api/bots/{bot['id']}",
            json={"scenario": "   "},
            headers={"Origin": ORIGIN},
        )
        assert cleared.status_code == 200
        blocked = client.patch(
            f"/api/bots/{bot['id']}/routines/{chosen['id']}",
            json={"weekdays": [1], "time": "08:00"},
            headers={"Origin": ORIGIN},
        )
        assert blocked.status_code == 400
        assert blocked.json() == SCENARIO_REQUIRED
        unchanged = [
            row
            for row in _account_routines(account_id)
            if str(row["id"]) == chosen["id"]
        ]
        assert list(unchanged[0]["weekdays"]) == [2, 4]
        assert unchanged[0]["time_of_day"] == time(10, 30)
        assert unchanged[0]["paused"] is False
        missing = client.get(f"/api/bots/{uuid.uuid4()}/routines")
        assert missing.status_code == 404
        assert missing.json() == NOT_FOUND
    with _client(other) as foreign:
        hidden = foreign.get(f"/api/bots/{bot['id']}/routines")
        assert hidden.status_code == 404
        assert hidden.json() == NOT_FOUND
    with TestClient(app) as anonymous:
        denied = anonymous.get(f"/api/bots/{bot['id']}/routines")
    assert denied.status_code == 401
    assert denied.json() == UNAUTHENTICATED


def _account_desktops(account_id: uuid.UUID) -> list[dict]:
    return [row for row in _rows(desktops) if row["account_id"] == account_id]


def _stopped(account_id: uuid.UUID) -> bool:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            return working_routines_stopped(connection, account_id)
    finally:
        engine.dispose()


def _login(email: str) -> TestClient:
    client = TestClient(app)
    logged = client.post(
        "/api/login",
        json={"email": email, "password": ACCEPTED},
        headers={"Origin": ORIGIN},
    )
    assert logged.status_code == 200
    return client


def test_working_desktop_logout_stops_routines_and_keeps_the_role(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_complete(*args: object, **kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", fail_complete)
    email = "working-logout@example.com"
    cookie, account_id = _open_account(email, confirmed=True)
    assert _stopped(account_id) is False
    working_computer = uuid.uuid4()
    other_computer = uuid.uuid4()
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "план")
        live = _create_routine(
            client, bot["id"], {"weekdays": [1, 3], "time": "09:00"}
        )
        held = _create_routine(
            client, bot["id"], {"weekdays": [2], "time": "10:00"}
        )
        assert live.status_code == 200
        assert held.status_code == 200
        paused = _pause(client, bot["id"], held.json()["id"], True)
        assert paused.status_code == 200
        posted = client.post(
            "/api/desktops",
            json={"id": str(working_computer)},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert posted.json() == {"id": str(working_computer), "working": True}
    bot_id = uuid.UUID(bot["id"])
    assert _chat_messages(bot_id) == []

    with _login(email) as other:
        second = other.post(
            "/api/desktops",
            json={"id": str(other_computer)},
            headers={"Origin": ORIGIN},
        )
        assert second.status_code == 200
        assert second.json() == {"id": str(other_computer), "working": False}
        signed_out = other.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200

    rows = {row["computer_id"]: row for row in _account_desktops(account_id)}
    assert rows[working_computer]["working"] is True
    assert rows[working_computer]["routines_stopped"] is False
    assert rows[working_computer]["session_id"] == uuid.UUID(cookie)
    assert rows[other_computer]["working"] is False
    assert rows[other_computer]["routines_stopped"] is False
    assert _stopped(account_id) is False

    with _client(cookie) as client:
        signed_out = client.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
    stopped_rows = {
        row["computer_id"]: row for row in _account_desktops(account_id)
    }
    assert len(stopped_rows) == 2
    working = stopped_rows[working_computer]
    assert working["working"] is True
    assert working["routines_stopped"] is True
    assert working["session_id"] is None
    assert stopped_rows[other_computer]["working"] is False
    assert stopped_rows[other_computer]["routines_stopped"] is False
    stored = {row["time_of_day"]: row for row in _account_routines(account_id)}
    assert stored[time(9, 0)]["paused"] is False
    assert stored[time(10, 0)]["paused"] is True
    assert starts_when_due(stored[time(9, 0)]) is True
    assert starts_when_due(stored[time(10, 0)]) is False
    stopped = _stopped(account_id)
    assert stopped is True
    assert routine_may_start(stored[time(9, 0)], stopped) is False
    assert routine_may_start(stored[time(10, 0)], stopped) is False
    assert _chat_messages(bot_id) == []

    with _login(email) as resumed:
        pulsed = resumed.post(
            f"/api/desktops/{working_computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
        resumed_rows = {
            row["computer_id"]: row for row in _account_desktops(account_id)
        }
        assert resumed_rows[working_computer]["working"] is True
        assert resumed_rows[working_computer]["routines_stopped"] is False
        pulse_session = resumed.cookies.get("session")
        assert isinstance(pulse_session, str)
        assert resumed_rows[working_computer]["session_id"] == uuid.UUID(pulse_session)
        after_pulse = {
            row["time_of_day"]: row for row in _account_routines(account_id)
        }
        assert after_pulse[time(9, 0)]["paused"] is False
        assert after_pulse[time(10, 0)]["paused"] is True
        running = _stopped(account_id)
        assert running is False
        assert routine_may_start(after_pulse[time(9, 0)], running) is True
        assert routine_may_start(after_pulse[time(10, 0)], running) is False
        assert _chat_messages(bot_id) == []
        signed_out = resumed.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
    assert _stopped(account_id) is True
    with _login(email) as returned:
        posted = returned.post(
            "/api/desktops",
            json={"id": str(working_computer)},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert posted.json() == {"id": str(working_computer), "working": True}
    restored = {
        row["computer_id"]: row for row in _account_desktops(account_id)
    }
    assert restored[working_computer]["working"] is True
    assert restored[working_computer]["routines_stopped"] is False
    finished = {row["time_of_day"]: row for row in _account_routines(account_id)}
    assert finished[time(10, 0)]["paused"] is True
    allowed = _stopped(account_id)
    assert allowed is False
    assert routine_may_start(finished[time(9, 0)], allowed) is True
    assert routine_may_start(finished[time(10, 0)], allowed) is False
    assert _chat_messages(bot_id) == []


DUE = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
CLAIM_AT = DUE + timedelta(seconds=59)
SKIP_AT = DUE + timedelta(seconds=60)
SKIP_TEXT = "Рутина пропущена: рабочий десктоп не забрал запуск."
STUB_TEXT = "Сейчас я на заглушке агрегатора и не вызываю модель."
EMPTY_MODEL_TEXT = "Ответ модели не пришёл."
QUOTA_EXHAUSTED_TEXT = "Квота на период закончилась."


def _refuse_aggregator(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_complete(*args: object, **kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", fail_complete)


def _settle(moment: datetime) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            settle_due(connection, moment)
    finally:
        engine.dispose()


def _runs_for(routine_id: str) -> list[dict]:
    parsed = uuid.UUID(routine_id)
    return [row for row in _rows(routine_runs) if row["routine_id"] == parsed]


def _stored_clock(routine_id: str) -> None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            shown = connection.execute(
                text("SELECT time_of_day::text FROM routines WHERE id = :id"),
                {"id": uuid.UUID(routine_id)},
            ).scalar_one()
            stored_type = connection.execute(
                text(
                    "SELECT pg_typeof(time_of_day)::text"
                    " FROM routines WHERE id = :id"
                ),
                {"id": uuid.UUID(routine_id)},
            ).scalar_one()
    finally:
        engine.dispose()
    assert shown == "09:00:00"
    assert stored_type == "time without time zone"


def _prepare_due_routine(
    email: str, *, paused: bool = False
) -> tuple[str, uuid.UUID, uuid.UUID, dict, dict]:
    cookie, account_id = _open_account(email, confirmed=True)
    computer = uuid.uuid4()
    with _client(cookie) as client:
        bot = _create_bot(client, "Искра", "план")
        created = _create_routine(
            client, bot["id"], {"weekdays": [1, 3, 5], "time": "09:00"}
        )
        assert created.status_code == 200
        assert created.json()["paused"] is False
        if paused:
            held = _pause(client, bot["id"], created.json()["id"], True)
            assert held.status_code == 200
        posted = client.post(
            "/api/desktops",
            json={"id": str(computer)},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert posted.json() == {"id": str(computer), "working": True}
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
    return cookie, account_id, computer, bot, created.json()


def test_claim_inside_the_window_takes_the_run_and_writes_no_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id, computer, bot, created = _prepare_due_routine(
        "claim-window@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    monkeypatch.setattr("server.routines.utc_now", lambda: CLAIM_AT)
    with _client(cookie) as client:
        claimed = client.post(
            f"/api/desktops/{computer}/routine-run",
            headers={"Origin": ORIGIN},
        )
    assert claimed.status_code == 200
    assert tuple(claimed.json()) == ("id", "routine_id", "bot_id")
    assert claimed.json()["routine_id"] == created["id"]
    assert claimed.json()["bot_id"] == bot["id"]
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["id"] == uuid.UUID(claimed.json()["id"])
    assert runs[0]["status"] == "claimed"
    assert runs[0]["claimed_at"] == CLAIM_AT
    assert runs[0]["due_at"] == DUE
    assert runs[0]["local_date"] == date(2026, 11, 18)
    spoken = _chat_messages(bot_id)
    assert [row["role"] for row in spoken] == ["assistant"]
    assert spoken[0]["text"] == STUB_TEXT
    assert all(row["code"] != "routine_skipped" for row in spoken)
    assert _session(cookie)["last_request_at"] == CLAIM_AT
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    assert _chat_messages(bot_id) == spoken
    stored = _account_routines(_account_id)[0]
    assert stored["time_of_day"] == time(9, 0)
    assert stored["time_of_day"].tzinfo is None
    _stored_clock(created["id"])


def test_missing_claim_writes_one_skip_for_that_local_date(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    _cookie, account_id, _computer, bot, created = _prepare_due_routine(
        "skip-window@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _settle(DUE - timedelta(seconds=1))
    assert _runs_for(created["id"]) == []
    assert _chat_messages(bot_id) == []
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["claimed_at"] is None
    assert opened[0]["due_at"] == DUE
    assert opened[0]["local_date"] == date(2026, 11, 18)
    assert _chat_messages(bot_id) == []
    _settle(CLAIM_AT)
    assert _runs_for(created["id"])[0]["status"] == "pending"
    assert _chat_messages(bot_id) == []
    _settle(SKIP_AT)
    skipped = _runs_for(created["id"])
    assert len(skipped) == 1
    assert skipped[0]["status"] == "skipped"
    assert skipped[0]["due_at"] == DUE
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "error"
    assert messages[0]["code"] == "routine_skipped"
    assert messages[0]["text"] == SKIP_TEXT
    chat = next(row for row in _rows(chats) if row["bot_id"] == bot_id)
    assert chat["updated_at"] == messages[0]["updated_at"]
    _settle(SKIP_AT + timedelta(hours=3))
    assert _runs_for(created["id"]) == skipped
    assert _chat_messages(bot_id) == messages
    stored = _account_routines(account_id)[0]
    assert stored["time_of_day"] == time(9, 0)
    assert stored["time_of_day"].tzinfo is None
    _stored_clock(created["id"])
    _late_cookie, _late_account, _late_computer, late_bot, late = _prepare_due_routine(
        "late-skip@example.com"
    )
    late_bot_id = uuid.UUID(late_bot["id"])
    _settle(DUE)
    late_opened = _runs_for(late["id"])
    assert len(late_opened) == 1
    assert late_opened[0]["status"] == "pending"
    assert _chat_messages(late_bot_id) == []
    _settle(SKIP_AT)
    late_runs = _runs_for(late["id"])
    assert len(late_runs) == 1
    assert late_runs[0]["status"] == "skipped"
    assert late_runs[0]["due_at"] == DUE
    late_messages = _chat_messages(late_bot_id)
    assert len(late_messages) == 1
    assert late_messages[0]["text"] == SKIP_TEXT
    _settle(SKIP_AT + timedelta(hours=3))
    assert _runs_for(late["id"]) == late_runs
    assert _chat_messages(late_bot_id) == late_messages


def test_pause_and_logout_hold_open_neither_a_run_nor_a_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    _cookie, paused_account, _computer, paused_bot, paused = _prepare_due_routine(
        "paused-window@example.com", paused=True
    )
    paused_bot_id = uuid.UUID(paused_bot["id"])
    _settle(SKIP_AT)
    paused_runs = _runs_for(paused["id"])
    assert len(paused_runs) == 1
    assert paused_runs[0]["status"] == "cancelled"
    assert _chat_messages(paused_bot_id) == []
    assert _account_routines(paused_account)[0]["paused"] is True

    cookie, held_account, _computer, held_bot, held = _prepare_due_routine(
        "stopped-window@example.com"
    )
    held_bot_id = uuid.UUID(held_bot["id"])
    with _client(cookie) as client:
        signed_out = client.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
    desktop = next(
        row for row in _rows(desktops) if row["account_id"] == held_account
    )
    assert desktop["working"] is True
    assert desktop["routines_stopped"] is True
    assert desktop["timezone"] == "Europe/Moscow"
    _settle(DUE)
    _settle(SKIP_AT)
    held_runs = _runs_for(held["id"])
    assert len(held_runs) == 1
    assert held_runs[0]["status"] == "cancelled"
    assert _chat_messages(held_bot_id) == []
    assert _account_routines(held_account)[0]["paused"] is False


def test_unpause_after_the_minute_does_not_write_a_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, _computer, bot, created = _prepare_due_routine(
        "unpause-after@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    with _client(cookie) as client:
        paused = _pause(client, bot["id"], created["id"], True)
        assert paused.status_code == 200
    _settle(DUE - timedelta(seconds=1))
    _settle(SKIP_AT)
    marked = _runs_for(created["id"])
    assert len(marked) == 1
    assert marked[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    with _client(cookie) as client:
        resumed = _pause(client, bot["id"], created["id"], False)
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False
    _settle(SKIP_AT + timedelta(hours=3))
    kept = _runs_for(created["id"])
    assert len(kept) == 1
    assert kept[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    assert _account_routines(account_id)[0]["paused"] is False


def test_sign_in_after_the_minute_does_not_write_a_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    email = "signin-after@example.com"
    cookie, account_id, computer, bot, created = _prepare_due_routine(email)
    bot_id = uuid.UUID(bot["id"])
    with _client(cookie) as client:
        signed_out = client.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
    _settle(DUE)
    _settle(SKIP_AT)
    marked = _runs_for(created["id"])
    assert len(marked) == 1
    assert marked[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    with _login(email) as client:
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
    desktop = next(
        row for row in _rows(desktops) if row["account_id"] == account_id
    )
    assert desktop["working"] is True
    assert desktop["routines_stopped"] is False
    _settle(SKIP_AT + timedelta(hours=3))
    kept = _runs_for(created["id"])
    assert len(kept) == 1
    assert kept[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    assert _account_routines(account_id)[0]["paused"] is False


def test_deleting_a_routine_removes_its_run_and_frees_the_slot(
    database: None,
) -> None:
    assert development_tariff.routines == 5
    cookie, account_id = _open_account("delete-run@example.com", confirmed=True)
    with _client(cookie) as client:
        first = _create_bot(client, "Первый", "сценарий")
        second = _create_bot(client, "Второй", "другой сценарий")
        created_ids: list[str] = []
        for index in range(development_tariff.routines):
            created = _create_routine(
                client,
                first["id"],
                {"weekdays": [index + 1], "time": f"09:{index:02d}"},
            )
            assert created.status_code == 200
            created_ids.append(created.json()["id"])
        blocked = _create_routine(
            client, second["id"], {"weekdays": [6], "time": "10:00"}
        )
        assert blocked.status_code == 400
        assert blocked.json() == ROUTINE_LIMIT
    routine_id = uuid.UUID(created_ids[0])
    bot_id = uuid.UUID(first["id"])
    chat = next(row for row in _rows(chats) if row["bot_id"] == bot_id)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                routine_runs.insert().values(
                    id=uuid.uuid4(),
                    routine_id=routine_id,
                    local_date=date(2026, 11, 18),
                    due_at=DUE,
                    status="skipped",
                )
            )
            connection.execute(
                messages.insert().values(
                    id=uuid.uuid4(),
                    chat_id=chat["id"],
                    role="error",
                    text=SKIP_TEXT,
                    code="routine_skipped",
                    updated_at=SKIP_AT,
                )
            )
    finally:
        engine.dispose()
    assert len(_runs_for(created_ids[0])) == 1
    with _client(cookie) as client:
        removed = client.delete(
            f"/api/bots/{first['id']}/routines/{created_ids[0]}",
            headers={"Origin": ORIGIN},
        )
        assert removed.status_code == 200
        assert removed.json() == {}
        assert len(_account_routines(account_id)) == 4
        again = _create_routine(
            client, second["id"], {"weekdays": [6], "time": "10:00"}
        )
        assert again.status_code == 200
        assert again.json()["weekdays"] == [6]
        assert again.json()["time"] == "10:00"
    assert _runs_for(created_ids[0]) == []
    assert len(_account_routines(account_id)) == 5
    kept = _chat_messages(bot_id)
    assert len(kept) == 1
    assert kept[0]["text"] == SKIP_TEXT


BEFORE_DUE = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)
STILL_BEFORE = datetime(2026, 11, 18, 5, 30, tzinfo=timezone.utc)
AFTER_WINDOW = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)
NEXT_DUE = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)


def test_first_settle_after_the_window_writes_one_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    _cookie, _account_id, _computer, bot, created = _prepare_due_routine(
        "late-first-sight@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _settle(SKIP_AT)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert runs[0]["due_at"] == DUE
    assert runs[0]["local_date"] == date(2026, 11, 18)
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "error"
    assert messages[0]["code"] == "routine_skipped"
    assert messages[0]["text"] == SKIP_TEXT
    _settle(SKIP_AT + timedelta(hours=3))
    assert _runs_for(created["id"]) == runs
    assert _chat_messages(bot_id) == messages


def test_pause_before_the_minute_and_unpause_after_keeps_cancelled(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, _account_id, _computer, bot, created = _prepare_due_routine(
        "cancel-pause@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    monkeypatch.setattr("server.bots.utc_now", lambda: BEFORE_DUE)
    with _client(cookie) as client:
        paused = _pause(client, bot["id"], created["id"], True)
        assert paused.status_code == 200
    marked = _runs_for(created["id"])
    assert len(marked) == 1
    assert marked[0]["status"] == "cancelled"
    assert marked[0]["local_date"] == date(2026, 11, 18)
    assert _chat_messages(bot_id) == []
    monkeypatch.setattr("server.bots.utc_now", lambda: AFTER_WINDOW)
    with _client(cookie) as client:
        resumed = _pause(client, bot["id"], created["id"], False)
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False
    _settle(AFTER_WINDOW)
    kept = _runs_for(created["id"])
    assert len(kept) == 1
    assert kept[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []


def test_logout_before_the_minute_and_pulse_after_keeps_cancelled(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    email = "cancel-logout@example.com"
    cookie, account_id, computer, bot, created = _prepare_due_routine(email)
    bot_id = uuid.UUID(bot["id"])
    monkeypatch.setattr("server.auth.utc_now", lambda: BEFORE_DUE)
    with _client(cookie) as client:
        signed_out = client.post("/api/logout", headers={"Origin": ORIGIN})
        assert signed_out.status_code == 200
    marked = _runs_for(created["id"])
    assert len(marked) == 1
    assert marked[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    monkeypatch.setattr("server.auth.utc_now", lambda: AFTER_WINDOW)
    monkeypatch.setattr("server.desktops.utc_now", lambda: AFTER_WINDOW)
    with _login(email) as client:
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
    desktop = next(
        row for row in _rows(desktops) if row["account_id"] == account_id
    )
    assert desktop["working"] is True
    assert desktop["routines_stopped"] is False
    _settle(AFTER_WINDOW)
    kept = _runs_for(created["id"])
    assert len(kept) == 1
    assert kept[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []


def test_unpause_before_the_minute_opens_that_minute(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, _account_id, _computer, bot, created = _prepare_due_routine(
        "reopen-minute@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    monkeypatch.setattr("server.bots.utc_now", lambda: BEFORE_DUE)
    with _client(cookie) as client:
        paused = _pause(client, bot["id"], created["id"], True)
        assert paused.status_code == 200
    assert _runs_for(created["id"])[0]["status"] == "cancelled"
    monkeypatch.setattr("server.bots.utc_now", lambda: STILL_BEFORE)
    with _client(cookie) as client:
        resumed = _pause(client, bot["id"], created["id"], False)
        assert resumed.status_code == 200
    assert _runs_for(created["id"]) == []
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["due_at"] == DUE
    assert _chat_messages(bot_id) == []


def test_next_local_date_opens_after_a_cancelled_mark(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, _account_id, computer, bot, created = _prepare_due_routine(
        "next-date@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    monkeypatch.setattr("server.bots.utc_now", lambda: BEFORE_DUE)
    with _client(cookie) as client:
        paused = _pause(client, bot["id"], created["id"], True)
        assert paused.status_code == 200
    monkeypatch.setattr("server.bots.utc_now", lambda: AFTER_WINDOW)
    with _client(cookie) as client:
        resumed = _pause(client, bot["id"], created["id"], False)
        assert resumed.status_code == 200
    _pulse_at(cookie, computer, NEXT_DUE, monkeypatch)
    _settle(NEXT_DUE)
    runs = {row["local_date"]: row for row in _runs_for(created["id"])}
    assert runs[date(2026, 11, 18)]["status"] == "cancelled"
    assert runs[date(2026, 11, 20)]["status"] == "pending"
    assert runs[date(2026, 11, 20)]["due_at"] == NEXT_DUE
    assert _chat_messages(bot_id) == []


STALE_PULSE = datetime(2026, 11, 18, 5, 29, tzinfo=timezone.utc)


def _pulse_at(
    cookie: str,
    computer: uuid.UUID,
    moment: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("server.desktops.utc_now", lambda: moment)
    with _client(cookie) as client:
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200


def _set_desktop(account_id: uuid.UUID, **values: object) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                desktops.update()
                .where(desktops.c.account_id == account_id)
                .values(**values)
            )
    finally:
        engine.dispose()


def test_stale_pulse_skips_at_the_minute_and_claim_returns_nothing(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        "stale-pulse@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _pulse_at(cookie, computer, STALE_PULSE, monkeypatch)
    stored = next(row for row in _rows(desktops) if row["account_id"] == account_id)
    assert stored["last_pulse_at"] == STALE_PULSE
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert runs[0]["due_at"] == DUE
    assert runs[0]["local_date"] == date(2026, 11, 18)
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "error"
    assert messages[0]["code"] == "routine_skipped"
    assert messages[0]["text"] == SKIP_TEXT
    monkeypatch.setattr("server.routines.utc_now", lambda: DUE)
    with _client(cookie) as client:
        claimed = client.post(
            f"/api/desktops/{computer}/routine-run",
            headers={"Origin": ORIGIN},
        )
    assert claimed.status_code == 204
    assert claimed.content == b""
    _settle(SKIP_AT)
    assert _runs_for(created["id"]) == runs
    assert _chat_messages(bot_id) == messages


def test_missing_working_desktop_skips_at_the_minute(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    _cookie, account_id, _computer, bot, created = _prepare_due_routine(
        "no-working@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_desktop(account_id, working=False)
    stored = next(row for row in _rows(desktops) if row["account_id"] == account_id)
    assert stored["working"] is False
    assert stored["timezone"] == "Europe/Moscow"
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert runs[0]["due_at"] == DUE
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["text"] == SKIP_TEXT
    _settle(SKIP_AT)
    assert _runs_for(created["id"]) == runs
    assert _chat_messages(bot_id) == messages


def test_fresh_pulse_opens_the_claim_window(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        "fresh-pulse@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _pulse_at(cookie, computer, DUE, monkeypatch)
    stored = next(row for row in _rows(desktops) if row["account_id"] == account_id)
    assert stored["last_pulse_at"] == DUE
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["due_at"] == DUE
    assert _chat_messages(bot_id) == []
    _settle(SKIP_AT)
    skipped = _runs_for(created["id"])
    assert len(skipped) == 1
    assert skipped[0]["status"] == "skipped"
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["text"] == SKIP_TEXT


def test_paused_routine_with_a_stale_pulse_writes_no_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        "paused-stale@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _pulse_at(cookie, computer, STALE_PULSE, monkeypatch)
    monkeypatch.setattr("server.bots.utc_now", lambda: STALE_PULSE)
    with _client(cookie) as client:
        paused = _pause(client, bot["id"], created["id"], True)
        assert paused.status_code == 200
        assert paused.json()["paused"] is True
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []


def test_account_without_an_accepted_timezone_writes_no_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    _cookie, account_id, _computer, bot, created = _prepare_due_routine(
        "no-zone@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_desktop(account_id, working=False, timezone=None)
    _settle(DUE)
    _settle(SKIP_AT)
    assert _runs_for(created["id"]) == []
    assert _chat_messages(bot_id) == []


SCENARIO_FAILURE = "Запуск завершён: сценария недостаточно."
SITE_FAILURE = "Запуск завершён: сайт не открылся."
LOGIN_FAILURE = "Запуск завершён: вход не удался."
UNKNOWN_RUN_REASON = {
    "code": "invalid_request",
    "message": "Неизвестная причина запуска.",
}


def _set_scenario(cookie: str, bot_id: str, scenario: str) -> None:
    with _client(cookie) as client:
        updated = client.patch(
            f"/api/bots/{bot_id}",
            json={"scenario": scenario},
            headers={"Origin": ORIGIN},
        )
        assert updated.status_code == 200
        assert updated.json()["scenario"] == scenario


def _claim(
    cookie: str,
    computer: uuid.UUID,
    moment: datetime,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("server.routines.utc_now", lambda: moment)
    with _client(cookie) as client:
        return client.post(
            f"/api/desktops/{computer}/routine-run",
            headers={"Origin": ORIGIN},
        )


def _report(
    cookie: str,
    computer: uuid.UUID,
    run_id: uuid.UUID,
    reason: str,
    moment: datetime,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("server.routines.utc_now", lambda: moment)
    with _client(cookie) as client:
        return client.post(
            f"/api/desktops/{computer}/routine-run",
            json={"id": str(run_id), "reason": reason},
            headers={"Origin": ORIGIN},
        )


def _one_failure(bot_id: uuid.UUID, code: str, text: str) -> list[dict]:
    messages = _chat_messages(bot_id)
    assert len(messages) == 1
    assert messages[0]["role"] == "error"
    assert messages[0]["code"] == code
    assert messages[0]["text"] == text
    return messages


def _stub_and_failure(bot_id: uuid.UUID, code: str, text: str) -> list[dict]:
    rows = _chat_messages(bot_id)
    assert len(rows) == 2
    by_role = {row["role"]: row for row in rows}
    assert by_role["assistant"]["text"] == STUB_TEXT
    assert by_role["assistant"]["code"] is None
    assert by_role["error"]["role"] == "error"
    assert by_role["error"]["code"] == code
    assert by_role["error"]["text"] == text
    return rows


def test_blank_scenario_fails_once_and_the_next_date_can_open(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, _account_id, computer, bot, created = _prepare_due_routine(
        "blank-run@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_scenario(cookie, bot["id"], " \n ")
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["due_at"] == DUE
    assert runs[0]["local_date"] == date(2026, 11, 18)
    messages = _one_failure(bot_id, "scenario_insufficient", SCENARIO_FAILURE)
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"]) == runs
    assert _chat_messages(bot_id) == messages
    _set_scenario(cookie, bot["id"], "план")
    _pulse_at(cookie, computer, NEXT_DUE, monkeypatch)
    _settle(NEXT_DUE)
    later = {row["local_date"]: row for row in _runs_for(created["id"])}
    assert later[date(2026, 11, 18)]["status"] == "failed"
    assert later[date(2026, 11, 20)]["status"] == "pending"
    assert later[date(2026, 11, 20)]["due_at"] == NEXT_DUE
    assert _chat_messages(bot_id) == messages


def test_claim_of_a_blank_scenario_fails_instead_of_claiming(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, _account_id, computer, bot, created = _prepare_due_routine(
        "blank-claim@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    assert _runs_for(created["id"])[0]["status"] == "pending"
    _set_scenario(cookie, bot["id"], "")
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 204
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    messages = _one_failure(bot_id, "scenario_insufficient", SCENARIO_FAILURE)
    again = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert again.status_code == 204
    assert _runs_for(created["id"]) == runs
    assert _chat_messages(bot_id) == messages


def _open_and_claim(
    email: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, uuid.UUID, dict, dict]:
    cookie, _account_id, computer, bot, created = _prepare_due_routine(email)
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 200
    assert claimed.json()["routine_id"] == created["id"]
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    return cookie, computer, bot, created


def test_site_not_opened_fails_the_claimed_run_once(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, _account_id, computer, bot, created = _prepare_due_routine(
        "site-failed@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    run_id = opened[0]["id"]
    early = _report(cookie, computer, run_id, "site_not_opened", DUE, monkeypatch)
    assert early.status_code == 204
    assert _runs_for(created["id"])[0]["status"] == "pending"
    assert _chat_messages(bot_id) == []
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 200
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    unknown = _report(cookie, computer, run_id, "dropped", DUE, monkeypatch)
    assert unknown.status_code == 400
    assert unknown.json() == UNKNOWN_RUN_REASON
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    assert [row["text"] for row in _chat_messages(bot_id)] == [STUB_TEXT]
    reported = _report(
        cookie, computer, run_id, "site_not_opened", DUE, monkeypatch
    )
    assert reported.status_code == 204
    assert _runs_for(created["id"])[0]["status"] == "failed"
    messages = _stub_and_failure(bot_id, "site_not_opened", SITE_FAILURE)
    repeated = _report(
        cookie, computer, run_id, "site_not_opened", SKIP_AT, monkeypatch
    )
    assert repeated.status_code == 204
    again = _claim(cookie, computer, SKIP_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "failed"
    assert _chat_messages(bot_id) == messages


def test_login_failed_fails_the_claimed_run_once(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, computer, bot, created = _open_and_claim(
        "login-failed@example.com", monkeypatch
    )
    bot_id = uuid.UUID(bot["id"])
    run_id = _runs_for(created["id"])[0]["id"]
    reported = _report(cookie, computer, run_id, "login_failed", DUE, monkeypatch)
    assert reported.status_code == 204
    assert _runs_for(created["id"])[0]["status"] == "failed"
    messages = _stub_and_failure(bot_id, "login_failed", LOGIN_FAILURE)
    repeated = _report(
        cookie, computer, run_id, "login_failed", SKIP_AT, monkeypatch
    )
    assert repeated.status_code == 204
    again = _claim(cookie, computer, SKIP_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "failed"
    assert _chat_messages(bot_id) == messages


VAULT_FAILURE = "Запуск завершён: без выбора записи войти нельзя."


def test_vault_choice_fails_the_claimed_run_once(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, computer, bot, created = _open_and_claim(
        "vault-choice@example.com", monkeypatch
    )
    bot_id = uuid.UUID(bot["id"])
    run_id = _runs_for(created["id"])[0]["id"]
    reported = _report(cookie, computer, run_id, "vault_choice", DUE, monkeypatch)
    assert reported.status_code == 204
    assert _runs_for(created["id"])[0]["status"] == "failed"
    messages = _stub_and_failure(bot_id, "vault_choice_required", VAULT_FAILURE)
    assert messages[-1]["text"] == VAULT_FAILURE
    assert "Какую запись" not in messages[-1]["text"]
    repeated = _report(
        cookie, computer, run_id, "vault_choice", SKIP_AT, monkeypatch
    )
    assert repeated.status_code == 204
    again = _claim(cookie, computer, SKIP_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "failed"
    assert _chat_messages(bot_id) == messages


NEW_SCENARIO = "Сначала прочитать почту.\nПотом записать итог."
USER_LINE = "Проверь почту"


def _period_id(account_id: uuid.UUID) -> uuid.UUID:
    periods = [
        row["id"]
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    ]
    assert len(periods) == 1
    return periods[0]


def _used(account_id: uuid.UUID) -> int:
    period_id = _period_id(account_id)
    counters = [
        row for row in _rows(quota_counters) if row["period_id"] == period_id
    ]
    assert len(counters) == 1
    return counters[0]["used"]


def _charges(account_id: uuid.UUID) -> list[dict]:
    period_id = _period_id(account_id)
    return [row for row in _rows(quota_charges) if row["period_id"] == period_id]


def _set_used(account_id: uuid.UUID, used: int) -> None:
    period_id = _period_id(account_id)
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


def _seed_chat(bot_id: uuid.UUID) -> None:
    chat = next(row for row in _rows(chats) if row["bot_id"] == bot_id)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                messages.insert().values(
                    id=uuid.uuid4(),
                    chat_id=chat["id"],
                    role="user",
                    text=USER_LINE,
                    code=None,
                    updated_at=DUE - timedelta(seconds=2),
                )
            )
            connection.execute(
                messages.insert().values(
                    id=uuid.uuid4(),
                    chat_id=chat["id"],
                    role="error",
                    text="старая ошибка",
                    code="earlier",
                    updated_at=DUE - timedelta(seconds=1),
                )
            )
    finally:
        engine.dispose()


def _open_pending(email: str, monkeypatch: pytest.MonkeyPatch):
    cookie, account_id, computer, bot, created = _prepare_due_routine(email)
    _set_scenario(cookie, bot["id"], NEW_SCENARIO)
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    assert _runs_for(created["id"])[0]["status"] == "pending"
    return cookie, account_id, computer, bot, created


def test_claimed_run_sends_the_current_scenario_and_charges_once(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []

    def spy(**kwargs: object) -> str:
        seen.append(kwargs)
        return STUB_TEXT

    monkeypatch.setattr("server.aggregator.complete", spy)
    cookie, account_id, computer, bot, created = _open_pending(
        "routine-stub@example.com", monkeypatch
    )
    bot_id = uuid.UUID(bot["id"])
    stored_bot = next(row for row in _rows(bots) if row["id"] == bot_id)
    _seed_chat(bot_id)
    assert development_tariff.model_requests == 500
    assert _used(account_id) == 0
    assert development_tariff.model_requests - _used(account_id) == 500
    assert _charges(account_id) == []
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 200
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    assert len(seen) == 1
    assert seen[0]["description"] == stored_bot["description"]
    assert seen[0]["scenario"] == NEW_SCENARIO
    assert seen[0]["desktop_available"] is True
    assert seen[0]["messages"] == [{"role": "user", "text": USER_LINE}]
    spoken = _chat_messages(bot_id)
    ordered = sorted(
        spoken, key=lambda row: (row["updated_at"], str(row["id"]))
    )
    assert [row["role"] for row in ordered] == ["user", "error", "assistant"]
    assert ordered[2]["text"] == STUB_TEXT
    assert ordered[2]["code"] is None
    assert ordered[2]["updated_at"] == DUE
    assert _used(account_id) == 1
    assert development_tariff.model_requests - _used(account_id) == 499
    charges = _charges(account_id)
    assert len(charges) == 1
    assert charges[0]["created_at"] == DUE
    again = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    assert len(seen) == 1
    assert _used(account_id) == 1
    assert _chat_messages(bot_id) == spoken


def test_empty_model_text_does_not_charge_the_claimed_run(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def empty(**kwargs: object) -> str:
        calls.append(kwargs)
        return ""

    monkeypatch.setattr("server.aggregator.complete", empty)
    cookie, account_id, computer, _bot, created = _open_pending(
        "routine-empty@example.com", monkeypatch
    )
    bot_id = uuid.UUID(_bot["id"])
    assert development_tariff.model_requests - _used(account_id) == 500
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 200
    assert len(calls) == 1
    assert calls[0]["scenario"] == NEW_SCENARIO
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    spoken = _chat_messages(bot_id)
    assert len(spoken) == 1
    assert spoken[0]["role"] == "error"
    assert spoken[0]["code"] == "empty_model_response"
    assert spoken[0]["text"] == EMPTY_MODEL_TEXT
    assert _used(account_id) == 0
    assert development_tariff.model_requests - _used(account_id) == 500
    assert _charges(account_id) == []
    again = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert len(calls) == 1
    assert _used(account_id) == 0
    assert _chat_messages(bot_id) == spoken


def test_exhausted_quota_stops_the_claimed_run_without_the_model(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(**kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", forbidden)
    cookie, account_id, computer, bot, created = _open_pending(
        "routine-quota@example.com", monkeypatch
    )
    bot_id = uuid.UUID(bot["id"])
    _set_used(account_id, development_tariff.model_requests)
    assert _used(account_id) == 500
    claimed = _claim(cookie, computer, DUE, monkeypatch)
    assert claimed.status_code == 200
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    spoken = _chat_messages(bot_id)
    assert len(spoken) == 1
    assert spoken[0]["role"] == "error"
    assert spoken[0]["code"] is None
    assert spoken[0]["text"] == QUOTA_EXHAUSTED_TEXT
    assert _used(account_id) == 500
    assert _charges(account_id) == []
    again = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert again.status_code == 204
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "claimed"
    assert _used(account_id) == 500
    assert _chat_messages(bot_id) == spoken


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


def test_inactive_period_cancels_the_minute_and_writes_no_skip(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        "inactive-period@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    periods = [
        row
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    ]
    assert len(periods) == 1
    assert periods[0]["active"] is True
    _set_period_active(account_id, False)
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "cancelled"
    assert runs[0]["due_at"] == DUE
    assert runs[0]["local_date"] == date(2026, 11, 18)
    assert _chat_messages(bot_id) == []
    _settle(SKIP_AT)
    assert _runs_for(created["id"])[0]["status"] == "cancelled"
    assert _chat_messages(bot_id) == []


def test_active_period_from_confirmation_still_opens_the_minute(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        "active-period@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    periods = [
        row
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    ]
    assert len(periods) == 1
    assert periods[0]["active"] is True
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["due_at"] == DUE
    assert _chat_messages(bot_id) == []


def test_two_routines_at_one_time_run_in_creation_order(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def spy(**kwargs: object) -> str:
        scenario = kwargs["scenario"]
        assert isinstance(scenario, str)
        seen.append(scenario)
        return STUB_TEXT

    monkeypatch.setattr("server.aggregator.complete", spy)
    cookie, account_id = _open_account("queue-order@example.com", confirmed=True)
    computer = uuid.uuid4()
    with _client(cookie) as client:
        first = _create_bot(client, "Первый", "сценарий первого")
        earlier = _create_routine(
            client, first["id"], {"weekdays": [1, 3, 5], "time": "09:00"}
        )
        assert earlier.status_code == 200
        second = _create_bot(client, "Второй", "сценарий второго")
        later = _create_routine(
            client, second["id"], {"weekdays": [1, 3, 5], "time": "09:00"}
        )
        assert later.status_code == 200
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
    stored = {row["bot_id"]: row for row in _account_routines(account_id)}
    assert (
        stored[uuid.UUID(first["id"])]["created_at"]
        < stored[uuid.UUID(second["id"])]["created_at"]
    )
    first_claim = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert first_claim.status_code == 200
    assert first_claim.json()["bot_id"] == first["id"]
    assert first_claim.json()["routine_id"] == earlier.json()["id"]
    assert _runs_for(earlier.json()["id"])[0]["status"] == "claimed"
    assert _runs_for(later.json()["id"])[0]["status"] == "pending"
    earlier_chat = _chat_messages(uuid.UUID(first["id"]))
    assert [row["role"] for row in earlier_chat] == ["assistant"]
    assert earlier_chat[0]["text"] == STUB_TEXT
    waiting = _chat_messages(uuid.UUID(second["id"]))
    assert [row["role"] for row in waiting] == ["error"]
    assert all(row["role"] != "assistant" for row in waiting)
    assert seen == ["сценарий первого"]
    second_claim = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert second_claim.status_code == 200
    assert second_claim.json()["bot_id"] == second["id"]
    assert second_claim.json()["routine_id"] == later.json()["id"]
    assert _runs_for(later.json()["id"])[0]["status"] == "claimed"
    later_chat = _chat_messages(uuid.UUID(second["id"]))
    assert any(
        row["role"] == "assistant" and row["text"] == STUB_TEXT for row in later_chat
    )
    assert seen == ["сценарий первого", "сценарий второго"]


def test_waiting_bot_is_told_once_who_holds_the_browser(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    monkeypatch.setattr("server.aggregator.complete", lambda **kwargs: STUB_TEXT)
    cookie, _account_id = _open_account("queue-note@example.com", confirmed=True)
    computer = uuid.uuid4()
    with _client(cookie) as client:
        first = _create_bot(client, "Первый", "сценарий первого")
        earlier = _create_routine(
            client, first["id"], {"weekdays": [1, 3, 5], "time": "09:00"}
        )
        assert earlier.status_code == 200
        second = _create_bot(client, "Второй", "сценарий второго")
        later = _create_routine(
            client, second["id"], {"weekdays": [1, 3, 5], "time": "09:00"}
        )
        assert later.status_code == 200
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
    claimed = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert claimed.status_code == 200
    assert set(claimed.json()) == {"id", "routine_id", "bot_id"}
    waiting = _chat_messages(uuid.UUID(second["id"]))
    assert len(waiting) == 1
    assert waiting[0]["role"] == "error"
    assert waiting[0]["code"] == "routine_queue"
    assert waiting[0]["text"] == "Ждёт бота «Первый»."
    holder = _chat_messages(uuid.UUID(first["id"]))
    assert [row["code"] for row in holder] == [None]
    assert all(row["code"] != "routine_queue" for row in holder)
    again = _claim(cookie, computer, CLAIM_AT, monkeypatch)
    assert again.status_code == 200
    assert again.json()["routine_id"] == later.json()["id"]
    after = sorted(
        _chat_messages(uuid.UUID(second["id"])),
        key=lambda row: (row["updated_at"], row["role"] != "error", str(row["id"])),
    )
    assert [row["code"] for row in after] == ["routine_queue", None]
    assert [row["text"] for row in after] == [
        "Ждёт бота «Первый».",
        STUB_TEXT,
    ]
    assert sum(row["code"] == "routine_queue" for row in after) == 1
