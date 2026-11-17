import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import NamedTuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.accounts import format_utc, touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.models import bots, chats

router = APIRouter()

COLORS = frozenset(
    {
        "#c85a12",
        "#1f7a4c",
        "#5c6570",
        "#3d7a62",
        "#a34b12",
        "#7d8b86",
    }
)
ICONS = frozenset({"mail", "chat", "table", "calendar", "box", "spark"})
DEFAULT_COLOR = "#c85a12"
DEFAULT_ICON = "spark"
NAME_LIMIT = 80
DESCRIPTION_LIMIT = 280
SCENARIO_LIMIT = 16000

INVALID_NAME = "Имя бота — от 1 до 80 символов."
INVALID_DESCRIPTION = "Описание длиннее 280 символов."
INVALID_SCENARIO = "Сценарий длиннее 16 000 символов."
INVALID_COLOR = "Такого цвета в наборе нет."
INVALID_ICON = "Такого значка в наборе нет."
INVALID_OBJECT = "Нужен JSON-объект."
NOT_FOUND = "Бот не найден."


class Opened(NamedTuple):
    connection: Connection
    session: RowMapping
    account: RowMapping
    now: datetime


def email_unconfirmed() -> JSONResponse:
    return error_response(403, "email_unconfirmed", "Почта ещё не подтверждена.")


def not_found() -> JSONResponse:
    return error_response(404, "not_found", NOT_FOUND)


def bot_document(row: RowMapping) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "color": row["color"],
        "icon": row["icon"],
        "description": row["description"],
        "scenario": row["scenario"],
        "updated_at": format_utc(row["updated_at"]),
    }


def parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def read_object(request: Request) -> dict[str, object] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", INVALID_OBJECT)
    if not isinstance(body, dict):
        return error_response(400, "invalid_request", INVALID_OBJECT)
    return body


def _invalid_name(value: object) -> JSONResponse | None:
    if not isinstance(value, str) or not 1 <= len(value) <= NAME_LIMIT:
        return error_response(400, "invalid_name", INVALID_NAME)
    return None


def _invalid_text(
    value: object, limit: int, code: str, message: str
) -> JSONResponse | None:
    if not isinstance(value, str) or len(value) > limit:
        return error_response(400, code, message)
    return None


def _invalid_choice(
    value: object, allowed: frozenset[str], message: str
) -> JSONResponse | None:
    if not isinstance(value, str) or value not in allowed:
        return error_response(400, "invalid_avatar", message)
    return None


def parse_create(body: dict[str, object]) -> dict[str, str] | JSONResponse:
    if "name" not in body:
        return error_response(400, "invalid_name", INVALID_NAME)
    name_error = _invalid_name(body["name"])
    if name_error is not None:
        return name_error
    description = body.get("description", "")
    description_error = _invalid_text(
        description, DESCRIPTION_LIMIT, "invalid_description", INVALID_DESCRIPTION
    )
    if description_error is not None:
        return description_error
    scenario = body.get("scenario", "")
    scenario_error = _invalid_text(
        scenario, SCENARIO_LIMIT, "invalid_scenario", INVALID_SCENARIO
    )
    if scenario_error is not None:
        return scenario_error
    if "color" in body:
        color_error = _invalid_choice(body["color"], COLORS, INVALID_COLOR)
        if color_error is not None:
            return color_error
        color = body["color"]
    else:
        color = DEFAULT_COLOR
    if "icon" in body:
        icon_error = _invalid_choice(body["icon"], ICONS, INVALID_ICON)
        if icon_error is not None:
            return icon_error
        icon = body["icon"]
    else:
        icon = DEFAULT_ICON
    return {
        "name": body["name"],
        "description": description,
        "scenario": scenario,
        "color": color,
        "icon": icon,
    }


def parse_patch(body: dict[str, object]) -> dict[str, str] | JSONResponse:
    changes: dict[str, str] = {}
    if "name" in body:
        name_error = _invalid_name(body["name"])
        if name_error is not None:
            return name_error
        changes["name"] = body["name"]
    if "description" in body:
        description_error = _invalid_text(
            body["description"],
            DESCRIPTION_LIMIT,
            "invalid_description",
            INVALID_DESCRIPTION,
        )
        if description_error is not None:
            return description_error
        changes["description"] = body["description"]
    if "scenario" in body:
        scenario_error = _invalid_text(
            body["scenario"],
            SCENARIO_LIMIT,
            "invalid_scenario",
            INVALID_SCENARIO,
        )
        if scenario_error is not None:
            return scenario_error
        changes["scenario"] = body["scenario"]
    if "color" in body:
        color_error = _invalid_choice(body["color"], COLORS, INVALID_COLOR)
        if color_error is not None:
            return color_error
        changes["color"] = body["color"]
    if "icon" in body:
        icon_error = _invalid_choice(body["icon"], ICONS, INVALID_ICON)
        if icon_error is not None:
            return icon_error
        changes["icon"] = body["icon"]
    return changes


def _load_bot(
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


@contextmanager
def open_confirmed(
    request: Request, *, mutating: bool
) -> Iterator[Opened | JSONResponse]:
    if mutating:
        rejected = origin_rejected(request)
        if rejected is not None:
            yield rejected
            return
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            yield unauthenticated()
            return
        session, account = current
        if account["email_confirmed_at"] is None:
            yield email_unconfirmed()
            return
        yield Opened(connection, session, account, now)


def refuse_bots(request: Request, *, mutating: bool) -> JSONResponse:
    if mutating:
        rejected = origin_rejected(request)
        if rejected is not None:
            return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            outcome = "anonymous"
        elif current[1]["email_confirmed_at"] is None:
            outcome = "unconfirmed"
        else:
            outcome = "confirmed"
    if outcome == "anonymous":
        return unauthenticated()
    if outcome == "unconfirmed":
        return email_unconfirmed()
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@router.get("/api/bots")
def list_bots(request: Request) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        rows = (
            opened.connection.execute(
                select(bots)
                .where(bots.c.account_id == opened.account["id"])
                .order_by(bots.c.updated_at.asc(), bots.c.id.asc())
            )
            .mappings()
            .all()
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse({"bots": [bot_document(row) for row in rows]})


@router.post("/api/bots")
async def create_bot(request: Request) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        body = await read_object(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = parse_create(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        bot_id = uuid.uuid4()
        opened.connection.execute(
            bots.insert().values(
                id=bot_id,
                account_id=opened.account["id"],
                name=parsed["name"],
                color=parsed["color"],
                icon=parsed["icon"],
                description=parsed["description"],
                scenario=parsed["scenario"],
                updated_at=opened.now,
            )
        )
        opened.connection.execute(
            chats.insert().values(
                id=uuid.uuid4(),
                bot_id=bot_id,
                updated_at=opened.now,
            )
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        row = (
            opened.connection.execute(select(bots).where(bots.c.id == bot_id))
            .mappings()
            .one()
        )
        return JSONResponse(bot_document(row))


@router.get("/api/bots/{bot_id}")
def read_bot(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        row = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if row is None:
            return not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(bot_document(row))


@router.patch("/api/bots/{bot_id}")
async def update_bot(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        body = await read_object(request)
        if isinstance(body, JSONResponse):
            return body
        changes = parse_patch(body)
        if isinstance(changes, JSONResponse):
            return changes
        existing = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if existing is None:
            return not_found()
        opened.connection.execute(
            bots.update()
            .where(
                bots.c.id == parsed_id,
                bots.c.account_id == opened.account["id"],
            )
            .values(**changes, updated_at=opened.now)
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        row = _load_bot(opened.connection, parsed_id, opened.account["id"])
        assert row is not None
        return JSONResponse(bot_document(row))


@router.get("/api/bots/{bot_id}/messages")
def list_messages(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=False)


@router.post("/api/bots/{bot_id}/messages")
def create_message(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=True)
