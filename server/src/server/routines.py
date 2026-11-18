import json
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Connection, RowMapping

import server.aggregator as aggregator
from server.accounts import touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.bots import (
    EMPTY_MODEL_CODE,
    EMPTY_MODEL_TEXT,
    QUOTA_EXHAUSTED_TEXT,
    _adapter_messages,
    _insert_message,
    _lock_quota_counter,
    not_found,
    open_confirmed,
    parse_id,
)
from server.config import development_tariff
from server.desktops import (
    desktop_not_found,
    find_working_desktop,
    parse_path_computer_id,
    timezone_name_accepted,
)
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
    subscription_periods,
)

router = APIRouter()

INVALID_ROUTINE = "Нужны дни недели и одно время."
SCENARIO_REQUIRED = "Без сценария рутину создать нельзя."
ROUTINE_LIMIT = "Лимит рутин исчерпан."
ROUTINE_NOT_FOUND = "Рутина не найдена."
INVALID_PAUSE = "Нужен признак паузы."
SKIP_TEXT = "Рутина пропущена: рабочий десктоп не забрал запуск."
SKIP_CODE = "routine_skipped"
SCENARIO_INSUFFICIENT = "scenario_insufficient"
SCENARIO_INSUFFICIENT_TEXT = "Запуск завершён: сценария недостаточно."
SITE_NOT_OPENED = "site_not_opened"
SITE_NOT_OPENED_TEXT = "Запуск завершён: сайт не открылся."
LOGIN_FAILED = "login_failed"
LOGIN_FAILED_TEXT = "Запуск завершён: вход не удался."
VAULT_CHOICE = "vault_choice"
VAULT_CHOICE_CODE = "vault_choice_required"
VAULT_CHOICE_TEXT = "Запуск завершён: без выбора записи войти нельзя."
UNKNOWN_RUN_REASON = "Неизвестная причина запуска."
RUN_FAILURES = {
    SITE_NOT_OPENED: (SITE_NOT_OPENED, SITE_NOT_OPENED_TEXT),
    LOGIN_FAILED: (LOGIN_FAILED, LOGIN_FAILED_TEXT),
    VAULT_CHOICE: (VAULT_CHOICE_CODE, VAULT_CHOICE_TEXT),
}
CLAIM_WAIT = timedelta(seconds=60)
PULSE_INTERVAL = timedelta(seconds=30)
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _invalid_routine() -> JSONResponse:
    return error_response(400, "invalid_routine", INVALID_ROUTINE)


def _scenario_required() -> JSONResponse:
    return error_response(400, "scenario_required", SCENARIO_REQUIRED)


def _routine_limit() -> JSONResponse:
    return error_response(400, "routine_limit", ROUTINE_LIMIT)


def _routine_not_found() -> JSONResponse:
    return error_response(404, "not_found", ROUTINE_NOT_FOUND)


def _invalid_pause() -> JSONResponse:
    return error_response(400, "invalid_request", INVALID_PAUSE)


def _unknown_run_reason() -> JSONResponse:
    return error_response(400, "invalid_request", UNKNOWN_RUN_REASON)


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def format_clock(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def appointed_utc(clock: time, zone_name: str, local_date: date) -> datetime:
    if not timezone_name_accepted(zone_name):
        raise ValueError(zone_name)
    local_clock = datetime.combine(local_date, clock, tzinfo=ZoneInfo(zone_name))
    return local_clock.astimezone(timezone.utc)


def working_desktop_zone(
    connection: Connection, account_id: uuid.UUID
) -> str | None:
    desktop = find_working_desktop(connection, account_id)
    if desktop is None or desktop["timezone"] is None:
        return None
    return desktop["timezone"]


def starts_when_due(row: Mapping[str, object]) -> bool:
    return row["paused"] is not True


def working_routines_stopped(
    connection: Connection, account_id: uuid.UUID
) -> bool:
    desktop = find_working_desktop(connection, account_id)
    if desktop is None:
        return False
    return desktop["routines_stopped"] is True


def routine_may_start(row: Mapping[str, object], stopped: bool) -> bool:
    return starts_when_due(row) and not stopped


def period_is_active(connection: Connection, account_id: uuid.UUID) -> bool:
    period = connection.execute(
        select(subscription_periods.c.id).where(
            subscription_periods.c.account_id == account_id,
            subscription_periods.c.active.is_(True),
        )
    ).first()
    return period is not None


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _accepted_zone(value: object) -> str | None:
    if not isinstance(value, str) or not timezone_name_accepted(value):
        return None
    return value


def _process_alive(last_pulse_at: datetime | None, now: datetime) -> bool:
    if last_pulse_at is None:
        return False
    return _aware(last_pulse_at) >= _aware(now) - PULSE_INTERVAL


def _schedule_desktop(
    connection: Connection, account_id: uuid.UUID, now: datetime
) -> tuple[str | None, bool]:
    desktop = find_working_desktop(connection, account_id)
    if desktop is not None:
        return (
            _accepted_zone(desktop["timezone"]),
            _process_alive(desktop["last_pulse_at"], now),
        )
    stored = connection.execute(
        select(desktops.c.timezone)
        .where(desktops.c.account_id == account_id)
        .order_by(desktops.c.id.asc())
    ).scalars()
    for zone in stored:
        accepted = _accepted_zone(zone)
        if accepted is not None:
            return accepted, False
    return None, False


def _write_chat_error(
    connection: Connection,
    bot_id: uuid.UUID,
    now: datetime,
    code: str,
    text: str,
) -> None:
    chat = (
        connection.execute(select(chats).where(chats.c.bot_id == bot_id))
        .mappings()
        .one()
    )
    connection.execute(
        messages.insert().values(
            id=uuid.uuid4(),
            chat_id=chat["id"],
            role="error",
            text=text,
            code=code,
            updated_at=now,
        )
    )
    connection.execute(
        chats.update().where(chats.c.id == chat["id"]).values(updated_at=now)
    )


def _write_skip(connection: Connection, bot_id: uuid.UUID, now: datetime) -> None:
    _write_chat_error(connection, bot_id, now, SKIP_CODE, SKIP_TEXT)


def scenario_insufficient(value: object) -> bool:
    return not isinstance(value, str) or value.strip() == ""


def _insert_failed(
    connection: Connection,
    routine_id: uuid.UUID,
    bot_id: uuid.UUID,
    local_date: date,
    due_at: datetime,
    now: datetime,
    code: str,
    text: str,
) -> None:
    connection.execute(
        routine_runs.insert().values(
            id=uuid.uuid4(),
            routine_id=routine_id,
            local_date=local_date,
            due_at=due_at,
            status="failed",
        )
    )
    _write_chat_error(connection, bot_id, now, code, text)


def _mark_failed(
    connection: Connection,
    run_id: uuid.UUID,
    from_status: str,
    bot_id: uuid.UUID,
    now: datetime,
    code: str,
    text: str,
) -> bool:
    updated = connection.execute(
        routine_runs.update()
        .where(
            routine_runs.c.id == run_id,
            routine_runs.c.status == from_status,
        )
        .values(status="failed")
    )
    if updated.rowcount != 1:
        return False
    _write_chat_error(connection, bot_id, now, code, text)
    return True


def _run_row(
    connection: Connection, routine_id: uuid.UUID, local_date: date
) -> RowMapping | None:
    return (
        connection.execute(
            select(routine_runs).where(
                routine_runs.c.routine_id == routine_id,
                routine_runs.c.local_date == local_date,
            )
        )
        .mappings()
        .one_or_none()
    )


def _occurrence(
    now: datetime,
    zone_name: str | None,
    weekdays: object,
    clock: time,
) -> tuple[datetime, date, datetime] | None:
    if zone_name is None or not timezone_name_accepted(zone_name):
        return None
    moment = _aware(now)
    local = moment.astimezone(ZoneInfo(zone_name))
    if local.isoweekday() not in {int(day) for day in weekdays}:
        return None
    due_at = appointed_utc(clock, zone_name, local.date())
    return moment, local.date(), due_at


def _ensure_cancelled(
    connection: Connection,
    routine_id: uuid.UUID,
    local_date: date,
    due_at: datetime,
) -> None:
    existing = _run_row(connection, routine_id, local_date)
    if existing is None:
        connection.execute(
            routine_runs.insert().values(
                id=uuid.uuid4(),
                routine_id=routine_id,
                local_date=local_date,
                due_at=due_at,
                status="cancelled",
            )
        )
        return
    if existing["status"] != "pending":
        return
    connection.execute(
        routine_runs.update()
        .where(
            routine_runs.c.id == existing["id"],
            routine_runs.c.status == "pending",
        )
        .values(status="cancelled")
    )


def _routines_for_account(
    connection: Connection, account_id: uuid.UUID
) -> list[RowMapping]:
    return list(
        connection.execute(
            select(
                routines.c.id,
                routines.c.weekdays,
                routines.c.time_of_day,
            )
            .join(bots, bots.c.id == routines.c.bot_id)
            .where(bots.c.account_id == account_id)
        )
        .mappings()
        .all()
    )


def cancel_routine_today(
    connection: Connection,
    routine: Mapping[str, object],
    account_id: uuid.UUID,
    now: datetime,
) -> None:
    found = _occurrence(
        now,
        working_desktop_zone(connection, account_id),
        routine["weekdays"],
        routine["time_of_day"],
    )
    if found is None:
        return
    _moment, local_date, due_at = found
    _ensure_cancelled(connection, routine["id"], local_date, due_at)


def cancel_account_routines(
    connection: Connection, account_id: uuid.UUID, now: datetime
) -> None:
    for routine in _routines_for_account(connection, account_id):
        cancel_routine_today(connection, routine, account_id, now)


def release_routine_today(
    connection: Connection,
    routine: Mapping[str, object],
    account_id: uuid.UUID,
    now: datetime,
) -> None:
    found = _occurrence(
        now,
        working_desktop_zone(connection, account_id),
        routine["weekdays"],
        routine["time_of_day"],
    )
    if found is None:
        return
    moment, local_date, due_at = found
    if moment >= due_at:
        return
    connection.execute(
        routine_runs.delete().where(
            routine_runs.c.routine_id == routine["id"],
            routine_runs.c.local_date == local_date,
            routine_runs.c.status == "cancelled",
        )
    )


def release_account_routines(
    connection: Connection, account_id: uuid.UUID, now: datetime
) -> None:
    for routine in _routines_for_account(connection, account_id):
        release_routine_today(connection, routine, account_id, now)


def _skip_unclaimed(
    connection: Connection,
    routine_id: uuid.UUID,
    bot_id: uuid.UUID,
    local_date: date,
    due_at: datetime,
    now: datetime,
    existing: RowMapping | None,
) -> None:
    if existing is None:
        connection.execute(
            routine_runs.insert().values(
                id=uuid.uuid4(),
                routine_id=routine_id,
                local_date=local_date,
                due_at=due_at,
                status="skipped",
            )
        )
        _write_skip(connection, bot_id, now)
        return
    if existing["status"] != "pending":
        return
    updated = connection.execute(
        routine_runs.update()
        .where(
            routine_runs.c.id == existing["id"],
            routine_runs.c.status == "pending",
        )
        .values(status="skipped")
    )
    if updated.rowcount == 1:
        _write_skip(connection, bot_id, now)


def _settle_occurrence(
    connection: Connection,
    routine_id: uuid.UUID,
    bot_id: uuid.UUID,
    local_date: date,
    due_at: datetime,
    now: datetime,
    *,
    allowed: bool,
    claimable: bool,
    scenario: object,
) -> None:
    if not allowed:
        if now >= due_at:
            _ensure_cancelled(connection, routine_id, local_date, due_at)
        return
    existing = _run_row(connection, routine_id, local_date)
    if existing is not None and existing["status"] != "pending":
        return
    if not claimable:
        if now >= due_at:
            _skip_unclaimed(
                connection,
                routine_id,
                bot_id,
                local_date,
                due_at,
                now,
                existing,
            )
        return
    deadline = due_at + CLAIM_WAIT
    if existing is None:
        if now >= deadline:
            _skip_unclaimed(
                connection,
                routine_id,
                bot_id,
                local_date,
                due_at,
                now,
                None,
            )
            return
        if now >= due_at:
            if scenario_insufficient(scenario):
                _insert_failed(
                    connection,
                    routine_id,
                    bot_id,
                    local_date,
                    due_at,
                    now,
                    SCENARIO_INSUFFICIENT,
                    SCENARIO_INSUFFICIENT_TEXT,
                )
                return
            connection.execute(
                routine_runs.insert().values(
                    id=uuid.uuid4(),
                    routine_id=routine_id,
                    local_date=local_date,
                    due_at=due_at,
                    status="pending",
                )
            )
        return
    if now < deadline:
        return
    _skip_unclaimed(
        connection,
        routine_id,
        bot_id,
        local_date,
        due_at,
        now,
        existing,
    )


def settle_due(connection: Connection, now: datetime) -> None:
    moment = _aware(now)
    rows = (
        connection.execute(
            select(
                routines.c.id,
                routines.c.bot_id,
                routines.c.weekdays,
                routines.c.time_of_day,
                routines.c.paused,
                bots.c.account_id,
                bots.c.scenario,
            ).join(bots, bots.c.id == routines.c.bot_id)
        )
        .mappings()
        .all()
    )
    for row in rows:
        account_id = row["account_id"]
        allowed = routine_may_start(
            row, working_routines_stopped(connection, account_id)
        ) and period_is_active(connection, account_id)
        zone, claimable = _schedule_desktop(connection, account_id, moment)
        if zone is None:
            continue
        local = moment.astimezone(ZoneInfo(zone))
        if local.isoweekday() not in {int(day) for day in row["weekdays"]}:
            continue
        due_at = appointed_utc(row["time_of_day"], zone, local.date())
        if moment < due_at and allowed:
            continue
        _settle_occurrence(
            connection,
            row["id"],
            row["bot_id"],
            local.date(),
            due_at,
            moment,
            allowed=allowed,
            claimable=claimable,
            scenario=row["scenario"],
        )


def claim_earliest_pending(
    connection: Connection, account_id: uuid.UUID, now: datetime
) -> RowMapping | None:
    moment = _aware(now)
    settle_due(connection, moment)
    if working_routines_stopped(connection, account_id):
        return None
    pending = (
        connection.execute(
            select(
                routine_runs.c.id,
                routine_runs.c.routine_id,
                routines.c.bot_id,
                bots.c.scenario,
            )
            .join(routines, routines.c.id == routine_runs.c.routine_id)
            .join(bots, bots.c.id == routines.c.bot_id)
            .where(
                bots.c.account_id == account_id,
                routines.c.paused.is_(False),
                routine_runs.c.status == "pending",
                routine_runs.c.due_at > moment - CLAIM_WAIT,
            )
            .order_by(routine_runs.c.due_at.asc(), routine_runs.c.id.asc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if pending is None:
        return None
    if scenario_insufficient(pending["scenario"]):
        _mark_failed(
            connection,
            pending["id"],
            "pending",
            pending["bot_id"],
            moment,
            SCENARIO_INSUFFICIENT,
            SCENARIO_INSUFFICIENT_TEXT,
        )
        return None
    updated = connection.execute(
        routine_runs.update()
        .where(
            routine_runs.c.id == pending["id"],
            routine_runs.c.status == "pending",
        )
        .values(status="claimed", claimed_at=moment)
    )
    if updated.rowcount != 1:
        return None
    _complete_claimed_run(connection, account_id, pending["bot_id"], moment)
    return pending


def _complete_claimed_run(
    connection: Connection,
    account_id: uuid.UUID,
    bot_id: uuid.UUID,
    now: datetime,
) -> None:
    bot = (
        connection.execute(select(bots).where(bots.c.id == bot_id))
        .mappings()
        .one()
    )
    scenario = bot["scenario"]
    if not isinstance(scenario, str) or scenario_insufficient(scenario):
        return
    chat = (
        connection.execute(select(chats).where(chats.c.bot_id == bot_id))
        .mappings()
        .one()
    )
    counter = _lock_quota_counter(connection, account_id)
    if counter["used"] >= development_tariff.model_requests:
        _insert_message(
            connection,
            chat["id"],
            "error",
            QUOTA_EXHAUSTED_TEXT,
            None,
            now,
        )
        connection.execute(
            chats.update().where(chats.c.id == chat["id"]).values(updated_at=now)
        )
        return
    reply = aggregator.complete(
        description=bot["description"],
        scenario=scenario,
        messages=_adapter_messages(connection, chat["id"]),
        desktop_available=True,
    )
    if reply == "":
        _insert_message(
            connection,
            chat["id"],
            "error",
            EMPTY_MODEL_TEXT,
            EMPTY_MODEL_CODE,
            now,
        )
    else:
        _insert_message(
            connection,
            chat["id"],
            "assistant",
            reply,
            None,
            now,
        )
        connection.execute(
            quota_charges.insert().values(
                id=uuid.uuid4(),
                period_id=counter["period_id"],
                created_at=now,
            )
        )
        connection.execute(
            quota_counters.update()
            .where(quota_counters.c.id == counter["id"])
            .values(used=counter["used"] + 1)
        )
    connection.execute(
        chats.update().where(chats.c.id == chat["id"]).values(updated_at=now)
    )


def routine_document(row: RowMapping) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "weekdays": sorted(int(day) for day in row["weekdays"]),
        "time": format_clock(row["time_of_day"]),
        "paused": row["paused"] is True,
    }


def _owned_bot(
    connection: Connection, bot_id: uuid.UUID, account_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(bots).where(
                bots.c.id == bot_id,
                bots.c.account_id == account_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def _lock_account_and_count_routines(
    connection: Connection, account_id: uuid.UUID
) -> int:
    connection.execute(
        select(accounts.c.id)
        .where(accounts.c.id == account_id)
        .with_for_update()
    ).scalar_one()
    return connection.execute(
        select(func.count(routines.c.id)).where(
            routines.c.bot_id.in_(
                select(bots.c.id).where(bots.c.account_id == account_id)
            )
        )
    ).scalar_one()


def _schedule(body: dict[str, object]) -> tuple[list[int], str] | JSONResponse:
    weekdays = body.get("weekdays")
    clock = body.get("time")
    if not isinstance(weekdays, list) or len(weekdays) == 0:
        return _invalid_routine()
    days: list[int] = []
    for day in weekdays:
        if isinstance(day, bool) or not isinstance(day, int):
            return _invalid_routine()
        if not 1 <= day <= 7:
            return _invalid_routine()
        days.append(day)
    if len(set(days)) != len(days):
        return _invalid_routine()
    if not isinstance(clock, str) or _CLOCK.fullmatch(clock) is None:
        return _invalid_routine()
    return sorted(days), clock


async def read_routine(
    request: Request,
) -> tuple[list[int], str] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_routine()
    if not isinstance(body, dict):
        return _invalid_routine()
    return _schedule(body)


@router.post("/api/bots/{bot_id}/routines")
async def create_routine(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        schedule = await read_routine(request)
        if isinstance(schedule, JSONResponse):
            return schedule
        days, clock = schedule
        bot = _owned_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        scenario = bot["scenario"]
        if not isinstance(scenario, str) or scenario.strip() == "":
            return _scenario_required()
        count = _lock_account_and_count_routines(
            opened.connection, opened.account["id"]
        )
        if count >= development_tariff.routines:
            return _routine_limit()
        routine_id = uuid.uuid4()
        opened.connection.execute(
            routines.insert().values(
                id=routine_id,
                bot_id=parsed_id,
                weekdays=days,
                time_of_day=_clock(clock),
                paused=False,
            )
        )
        row = (
            opened.connection.execute(
                select(routines).where(routines.c.id == routine_id)
            )
            .mappings()
            .one()
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(routine_document(row))


@router.get("/api/bots/{bot_id}/routines")
def list_routines(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _owned_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        rows = (
            opened.connection.execute(
                select(routines)
                .where(routines.c.bot_id == parsed_id)
                .order_by(routines.c.id.asc())
            )
            .mappings()
            .all()
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse([routine_document(row) for row in rows])


@router.delete("/api/bots/{bot_id}/routines/{routine_id}")
def delete_routine(
    request: Request, bot_id: str, routine_id: str
) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_bot = parse_id(bot_id)
        if parsed_bot is None:
            return not_found()
        bot = _owned_bot(opened.connection, parsed_bot, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_routine = parse_id(routine_id)
        if parsed_routine is None:
            return _routine_not_found()
        found = opened.connection.execute(
            select(routines.c.id).where(
                routines.c.id == parsed_routine,
                routines.c.bot_id == parsed_bot,
            )
        ).one_or_none()
        if found is None:
            return _routine_not_found()
        opened.connection.execute(
            routines.delete().where(
                routines.c.id == parsed_routine,
                routines.c.bot_id == parsed_bot,
            )
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse({})


async def read_routine_patch(
    request: Request,
) -> tuple[bool | None, tuple[list[int], str] | None] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_pause()
    if not isinstance(body, dict):
        return _invalid_pause()
    has_schedule = "weekdays" in body or "time" in body
    if "paused" not in body and not has_schedule:
        return _invalid_pause()
    paused: bool | None = None
    if "paused" in body:
        value = body["paused"]
        if not isinstance(value, bool):
            return _invalid_pause()
        paused = value
    schedule: tuple[list[int], str] | None = None
    if has_schedule:
        parsed = _schedule(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        schedule = parsed
    return paused, schedule


def _owned_routine(
    connection: Connection, routine_id: uuid.UUID, bot_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(routines).where(
                routines.c.id == routine_id,
                routines.c.bot_id == bot_id,
            )
        )
        .mappings()
        .one_or_none()
    )


@router.patch("/api/bots/{bot_id}/routines/{routine_id}")
async def update_routine_pause(
    request: Request, bot_id: str, routine_id: str
) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_bot = parse_id(bot_id)
        if parsed_bot is None:
            return not_found()
        patch = await read_routine_patch(request)
        if isinstance(patch, JSONResponse):
            return patch
        paused, schedule = patch
        bot = _owned_bot(opened.connection, parsed_bot, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_routine = parse_id(routine_id)
        if parsed_routine is None:
            return _routine_not_found()
        existing = _owned_routine(opened.connection, parsed_routine, parsed_bot)
        if existing is None:
            return _routine_not_found()
        if schedule is not None:
            scenario = bot["scenario"]
            if not isinstance(scenario, str) or scenario.strip() == "":
                return _scenario_required()
        values: dict[str, object] = {}
        if paused is not None:
            values["paused"] = paused
        if schedule is not None:
            days, clock = schedule
            values["weekdays"] = days
            values["time_of_day"] = _clock(clock)
        opened.connection.execute(
            routines.update()
            .where(
                routines.c.id == parsed_routine,
                routines.c.bot_id == parsed_bot,
            )
            .values(**values)
        )
        if paused is True:
            cancel_routine_today(
                opened.connection, existing, opened.account["id"], opened.now
            )
        elif paused is False:
            release_routine_today(
                opened.connection, existing, opened.account["id"], opened.now
            )
        row = _owned_routine(opened.connection, parsed_routine, parsed_bot)
        assert row is not None
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(routine_document(row))


def report_run_failure(
    connection: Connection,
    account_id: uuid.UUID,
    run_id: uuid.UUID | None,
    reason: str,
    now: datetime,
) -> None:
    if run_id is None:
        return
    failure = RUN_FAILURES.get(reason)
    if failure is None:
        return
    code, text = failure
    row = (
        connection.execute(
            select(
                routine_runs.c.id,
                routine_runs.c.status,
                routines.c.bot_id,
            )
            .join(routines, routines.c.id == routine_runs.c.routine_id)
            .join(bots, bots.c.id == routines.c.bot_id)
            .where(
                routine_runs.c.id == run_id,
                bots.c.account_id == account_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["status"] != "claimed":
        return
    _mark_failed(
        connection,
        row["id"],
        "claimed",
        row["bot_id"],
        _aware(now),
        code,
        text,
    )


async def _read_failure_report(
    request: Request,
) -> tuple[uuid.UUID | None, str] | None | JSONResponse:
    raw = await request.body()
    if raw.strip() == b"":
        return None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or "reason" not in body:
        return None
    reason = body.get("reason")
    if not isinstance(reason, str) or reason not in RUN_FAILURES:
        return _unknown_run_reason()
    raw_id = body.get("id")
    if not isinstance(raw_id, str):
        return None, reason
    try:
        return uuid.UUID(raw_id), reason
    except ValueError:
        return None, reason


@router.post("/api/desktops/{computer_id}/routine-run")
async def post_routine_run(request: Request, computer_id: str) -> Response:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    report = await _read_failure_report(request)
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        parsed = parse_path_computer_id(computer_id)
        if parsed is None:
            return desktop_not_found()
        session, account = current
        desktop = find_working_desktop(connection, account["id"])
        if desktop is None or desktop["computer_id"] != parsed:
            return desktop_not_found()
        if isinstance(report, JSONResponse):
            return report
        if report is not None:
            run_id, reason = report
            report_run_failure(connection, account["id"], run_id, reason, now)
            touch_session(connection, session["id"], now)
            return Response(status_code=204)
        claimed = claim_earliest_pending(connection, account["id"], now)
        touch_session(connection, session["id"], now)
        if claimed is None:
            return Response(status_code=204)
        return JSONResponse(
            {
                "id": str(claimed["id"]),
                "routine_id": str(claimed["routine_id"]),
                "bot_id": str(claimed["bot_id"]),
            }
        )
