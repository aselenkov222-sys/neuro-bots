import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from server.accounts import touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.models import desktops
from server.notices import mark_delivered, pending_notices

router = APIRouter()

INVALID_DESKTOP = "Нужен идентификатор десктопа."
NOT_FOUND_DESKTOP = "Десктоп не найден."
INVALID_TIMEZONE = "Неизвестная временная зона."
INVALID_NOTICES = "Нужны идентификаторы уведомлений."


def _find_desktop(
    connection: Connection, account_id: uuid.UUID, computer_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(desktops).where(
                desktops.c.account_id == account_id,
                desktops.c.computer_id == computer_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def find_working_desktop(
    connection: Connection, account_id: uuid.UUID
) -> RowMapping | None:
    return _working_desktop(connection, account_id)


def _working_desktop(
    connection: Connection, account_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(desktops).where(
                desktops.c.account_id == account_id,
                desktops.c.working.is_(True),
            )
        )
        .mappings()
        .one_or_none()
    )


def _insert_desktop(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    working: bool,
    now: datetime,
) -> bool:
    try:
        with connection.begin_nested():
            connection.execute(
                desktops.insert().values(
                    id=uuid.uuid4(),
                    account_id=account_id,
                    computer_id=computer_id,
                    working=working,
                    created_at=now,
                )
            )
    except IntegrityError:
        return False
    return True


def store_desktop(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    now: datetime,
) -> RowMapping:
    existing = _find_desktop(connection, account_id, computer_id)
    if existing is not None:
        return existing
    working = _working_desktop(connection, account_id) is None
    stored = None
    if _insert_desktop(connection, account_id, computer_id, working, now):
        stored = _find_desktop(connection, account_id, computer_id)
    else:
        stored = _find_desktop(connection, account_id, computer_id)
        if stored is None and working:
            if _insert_desktop(connection, account_id, computer_id, False, now):
                stored = _find_desktop(connection, account_id, computer_id)
            else:
                stored = _find_desktop(connection, account_id, computer_id)
    if stored is None:
        raise RuntimeError("desktop row was not stored")
    return stored


def remember_signed_in_session(
    connection: Connection,
    desktop_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime,
) -> None:
    current = (
        connection.execute(select(desktops).where(desktops.c.id == desktop_id))
        .mappings()
        .one()
    )
    was_stopped = current["routines_stopped"] is True
    connection.execute(
        desktops.update()
        .where(desktops.c.id == desktop_id)
        .values(session_id=session_id, routines_stopped=False)
    )
    if was_stopped:
        from server.routines import release_account_routines

        release_account_routines(connection, current["account_id"], now)


def stop_routines_for_working_logout(
    connection: Connection,
    account_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime,
) -> None:
    desktop = _working_desktop(connection, account_id)
    if desktop is None or desktop["session_id"] != session_id:
        return
    connection.execute(
        desktops.update()
        .where(desktops.c.id == desktop["id"])
        .values(session_id=None, routines_stopped=True)
    )
    from server.routines import cancel_account_routines

    cancel_account_routines(connection, account_id, now)


async def read_computer_id(request: Request) -> uuid.UUID | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", INVALID_DESKTOP)
    if not isinstance(body, dict):
        return error_response(400, "invalid_request", INVALID_DESKTOP)
    raw = body.get("id")
    if not isinstance(raw, str):
        return error_response(400, "invalid_request", INVALID_DESKTOP)
    try:
        return uuid.UUID(raw)
    except ValueError:
        return error_response(400, "invalid_request", INVALID_DESKTOP)


@router.post("/api/desktops")
async def post_desktop(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        computer_id = await read_computer_id(request)
        if isinstance(computer_id, JSONResponse):
            return computer_id
        session, account = current
        row = store_desktop(connection, account["id"], computer_id, now)
        remember_signed_in_session(connection, row["id"], session["id"], now)
        touch_session(connection, session["id"], now)
        document = {"id": str(row["computer_id"]), "working": row["working"]}
    return JSONResponse(document)


def desktop_not_found() -> JSONResponse:
    return error_response(404, "not_found", NOT_FOUND_DESKTOP)


def timezone_name_accepted(name: str) -> bool:
    if name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def parse_path_computer_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def read_timezone(request: Request) -> str | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_timezone", INVALID_TIMEZONE)
    if not isinstance(body, dict):
        return error_response(400, "invalid_timezone", INVALID_TIMEZONE)
    raw = body.get("timezone")
    if not isinstance(raw, str) or not timezone_name_accepted(raw):
        return error_response(400, "invalid_timezone", INVALID_TIMEZONE)
    return raw


@router.post("/api/desktops/{computer_id}/pulse")
async def post_pulse(request: Request, computer_id: str) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        parsed = parse_path_computer_id(computer_id)
        if parsed is None:
            return desktop_not_found()
        session, account = current
        row = _find_desktop(connection, account["id"], parsed)
        if row is None or row["working"] is not True:
            return desktop_not_found()
        timezone_name = await read_timezone(request)
        if isinstance(timezone_name, JSONResponse):
            return timezone_name
        was_stopped = row["routines_stopped"] is True
        connection.execute(
            desktops.update()
            .where(
                desktops.c.id == row["id"],
                desktops.c.account_id == account["id"],
                desktops.c.working.is_(True),
            )
            .values(
                timezone=timezone_name,
                last_pulse_at=now,
                session_id=session["id"],
                routines_stopped=False,
            )
        )
        if was_stopped:
            from server.routines import release_account_routines

            release_account_routines(connection, account["id"], now)
        touch_session(connection, session["id"], now)
    return JSONResponse({"timezone": timezone_name})


def _working_match(
    connection: Connection, account_id: uuid.UUID, computer_id: uuid.UUID
) -> RowMapping | None:
    row = find_working_desktop(connection, account_id)
    if row is None or row["computer_id"] != computer_id:
        return None
    return row


@router.get("/api/desktops/{computer_id}/notices")
def get_notices(request: Request, computer_id: str) -> JSONResponse:
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        parsed = parse_path_computer_id(computer_id)
        if parsed is None:
            return desktop_not_found()
        session, account = current
        if _working_match(connection, account["id"], parsed) is None:
            return desktop_not_found()
        notices = pending_notices(connection, account["id"])
        touch_session(connection, session["id"], now)
    return JSONResponse({"notices": notices})


async def _read_notice_ids(request: Request) -> list[uuid.UUID] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", INVALID_NOTICES)
    if not isinstance(body, dict):
        return error_response(400, "invalid_request", INVALID_NOTICES)
    raw = body.get("ids")
    if not isinstance(raw, list):
        return error_response(400, "invalid_request", INVALID_NOTICES)
    parsed: list[uuid.UUID] = []
    for item in raw:
        if not isinstance(item, str):
            return error_response(400, "invalid_request", INVALID_NOTICES)
        try:
            parsed.append(uuid.UUID(item))
        except ValueError:
            return error_response(400, "invalid_request", INVALID_NOTICES)
    return parsed


@router.post("/api/desktops/{computer_id}/notices")
async def post_notices(request: Request, computer_id: str) -> Response:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    notice_ids = await _read_notice_ids(request)
    if isinstance(notice_ids, JSONResponse):
        return notice_ids
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        parsed = parse_path_computer_id(computer_id)
        if parsed is None:
            return desktop_not_found()
        session, account = current
        if _working_match(connection, account["id"], parsed) is None:
            return desktop_not_found()
        mark_delivered(connection, account["id"], notice_ids, now)
        touch_session(connection, session["id"], now)
    return Response(status_code=204)
