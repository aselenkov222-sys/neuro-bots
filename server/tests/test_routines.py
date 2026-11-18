import subprocess
import sys
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import ARRAY, Boolean, Integer, Text, Time, Uuid, select, text

from server.accounts import create_account, create_session
from server.app import app
from server.config import development_tariff
from server.db import create_db_engine, metadata
from server.models import accounts, bots, chats, desktops, messages, routines, sessions
from server.routines import (
    appointed_utc,
    routine_may_start,
    starts_when_due,
    working_desktop_zone,
    working_routines_stopped,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
ROUTINE_COLUMNS = ("id", "bot_id", "weekdays", "time_of_day", "paused")
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
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == account_id)
                    .values(
                        email_confirmed_at=datetime(
                            2026, 11, 16, tzinfo=timezone.utc
                        )
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
        assert column.name not in {"scenario", "text", "title", "created_at"}
    assert isinstance(routines.c.paused.type, Boolean)
    assert routines.c.paused.nullable is False
    assert routines.c.paused.server_default.arg.text == "false"

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
