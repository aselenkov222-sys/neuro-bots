import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
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

router = APIRouter()

INVALID_DESKTOP = "Нужен идентификатор десктопа."


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
        touch_session(connection, session["id"], now)
        document = {"id": str(row["computer_id"]), "working": row["working"]}
    return JSONResponse(document)
