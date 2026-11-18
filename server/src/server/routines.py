import json
import re
import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.engine import Connection, RowMapping

from server.accounts import touch_session
from server.auth import error_response
from server.bots import not_found, open_confirmed, parse_id
from server.config import development_tariff
from server.desktops import find_working_desktop, timezone_name_accepted
from server.models import accounts, bots, routines

router = APIRouter()

INVALID_ROUTINE = "Нужны дни недели и одно время."
SCENARIO_REQUIRED = "Без сценария рутину создать нельзя."
ROUTINE_LIMIT = "Лимит рутин исчерпан."
ROUTINE_NOT_FOUND = "Рутина не найдена."
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _invalid_routine() -> JSONResponse:
    return error_response(400, "invalid_routine", INVALID_ROUTINE)


def _scenario_required() -> JSONResponse:
    return error_response(400, "scenario_required", SCENARIO_REQUIRED)


def _routine_limit() -> JSONResponse:
    return error_response(400, "routine_limit", ROUTINE_LIMIT)


def _routine_not_found() -> JSONResponse:
    return error_response(404, "not_found", ROUTINE_NOT_FOUND)


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


def routine_document(row: RowMapping) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "weekdays": sorted(int(day) for day in row["weekdays"]),
        "time": format_clock(row["time_of_day"]),
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


async def read_routine(
    request: Request,
) -> tuple[list[int], str] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_routine()
    if not isinstance(body, dict):
        return _invalid_routine()
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
