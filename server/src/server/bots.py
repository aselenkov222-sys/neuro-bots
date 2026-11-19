import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

import server.aggregator as aggregator
from server.accounts import format_utc, touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.browser_action import (
    bot_holds_browser,
    enqueue_open,
    hold_text,
    named_address,
    take_login_choice,
)
from server.dangerous import DANGEROUS_CODE, gate_state
from server.config import development_tariff
from server.desktops import find_working_desktop
from server.models import (
    bots,
    chats,
    messages,
    quota_charges,
    quota_counters,
    subscription_periods,
)

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
MESSAGE_LIMIT = 8000
INVALID_MESSAGE = "Сообщение — от 1 до 8 000 символов."
EMPTY_MODEL_TEXT = "Ответ модели не пришёл."
EMPTY_MODEL_CODE = "empty_model_response"
QUOTA_EXHAUSTED_TEXT = "Квота на период закончилась."
COMPUTER_UNAVAILABLE_TEXT = "компьютер недоступен"
CHAT_NOT_FOUND = "Чат не найден."
MAIN_KIND = "main"
EXTRA_KIND = "extra"


class Opened(NamedTuple):
    connection: Connection
    session: RowMapping
    account: RowMapping
    now: datetime


def email_unconfirmed() -> JSONResponse:
    return error_response(403, "email_unconfirmed", "Почта ещё не подтверждена.")


def not_found() -> JSONResponse:
    return error_response(404, "not_found", NOT_FOUND)


def chat_not_found() -> JSONResponse:
    return error_response(404, "not_found", CHAT_NOT_FOUND)


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


class QuotaCounterMissing(RuntimeError):
    pass


def main_chat(connection: Connection, bot_id: uuid.UUID) -> RowMapping:
    return (
        connection.execute(
            select(chats).where(
                chats.c.bot_id == bot_id,
                chats.c.kind == MAIN_KIND,
            )
        )
        .mappings()
        .one()
    )


def _owned_chat(
    connection: Connection, chat_id: uuid.UUID, bot_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(chats).where(
                chats.c.id == chat_id,
                chats.c.bot_id == bot_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def chat_document(row: RowMapping) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "created_at": format_utc(row["created_at"]),
        "updated_at": format_utc(row["updated_at"]),
    }


def _bot_chats(connection: Connection, bot_id: uuid.UUID) -> list[RowMapping]:
    rows = (
        connection.execute(select(chats).where(chats.c.bot_id == bot_id))
        .mappings()
        .all()
    )
    return sorted(
        rows,
        key=lambda row: (
            0 if row["kind"] == MAIN_KIND else 1,
            row["created_at"],
            row["id"],
        ),
    )


def _lock_quota_counter(
    connection: Connection, account_id: uuid.UUID
) -> RowMapping:
    period = (
        connection.execute(
            select(subscription_periods.c.id).where(
                subscription_periods.c.account_id == account_id,
                subscription_periods.c.active.is_(True),
            )
        )
        .scalars()
        .one_or_none()
    )
    if period is None:
        raise QuotaCounterMissing
    counter = (
        connection.execute(
            select(quota_counters)
            .where(quota_counters.c.period_id == period)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if counter is None:
        raise QuotaCounterMissing
    return counter


def _insert_message(
    connection: Connection,
    chat_id: uuid.UUID,
    role: str,
    text: str,
    code: str | None,
    updated_at: datetime,
) -> None:
    connection.execute(
        messages.insert().values(
            id=uuid.uuid4(),
            chat_id=chat_id,
            role=role,
            text=text,
            code=code,
            updated_at=updated_at,
        )
    )


def _messages_document(connection: Connection, chat_id: uuid.UUID) -> dict[str, object]:
    rows = (
        connection.execute(
            select(messages)
            .where(messages.c.chat_id == chat_id)
            .order_by(messages.c.updated_at.asc(), messages.c.id.asc())
        )
        .mappings()
        .all()
    )
    payload = [
        {
            "id": str(row["id"]),
            "role": row["role"],
            "text": row["text"],
            "code": row["code"],
            "updated_at": format_utc(row["updated_at"]),
        }
        for row in rows
    ]
    gated = [
        uuid.UUID(item["id"])
        for item in payload
        if item["code"] == DANGEROUS_CODE
    ]
    states = gate_state(connection, gated)
    for item in payload:
        state = states.get(uuid.UUID(item["id"]))
        if state is None:
            continue
        item["answer"] = state["answer"]
        item["pending"] = state["pending"]
    return {"messages": payload}


def _adapter_messages(connection: Connection, chat_id: uuid.UUID) -> list[dict[str, str]]:
    rows = (
        connection.execute(
            select(messages.c.role, messages.c.text)
            .where(
                messages.c.chat_id == chat_id,
                messages.c.role.in_(("user", "assistant")),
            )
            .order_by(messages.c.updated_at.asc(), messages.c.id.asc())
        )
        .mappings()
        .all()
    )
    return [{"role": row["role"], "text": row["text"]} for row in rows]


async def read_message_text(request: Request) -> str | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_message", INVALID_MESSAGE)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not 1 <= len(text) <= MESSAGE_LIMIT:
        return error_response(400, "invalid_message", INVALID_MESSAGE)
    return text


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
                kind=MAIN_KIND,
                created_at=opened.now,
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
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        chat = main_chat(opened.connection, parsed_id)
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(_messages_document(opened.connection, chat["id"]))


@router.post("/api/bots/{bot_id}/messages")
async def create_message(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        text = await read_message_text(request)
        if isinstance(text, JSONResponse):
            return text
        counter = _lock_quota_counter(opened.connection, opened.account["id"])
        chat = main_chat(opened.connection, parsed_id)
        _insert_message(
            opened.connection,
            chat["id"],
            "user",
            text,
            None,
            opened.now,
        )
        follow_at = opened.now + timedelta(microseconds=1)
        desktop = find_working_desktop(opened.connection, opened.account["id"])
        desktop_available = (
            desktop is not None and desktop["last_pulse_at"] is not None
        )
        address = named_address(text)
        chosen_url = None
        if (
            desktop is not None
            and desktop_available
            and counter["used"] < development_tariff.model_requests
        ):
            chosen_url = take_login_choice(
                account_id=opened.account["id"],
                computer_id=desktop["computer_id"],
                bot_id=parsed_id,
                text=text,
            )
        if counter["used"] >= development_tariff.model_requests:
            _insert_message(
                opened.connection,
                chat["id"],
                "error",
                QUOTA_EXHAUSTED_TEXT,
                None,
                follow_at,
            )
        elif address is not None and not desktop_available:
            _insert_message(
                opened.connection,
                chat["id"],
                "error",
                COMPUTER_UNAVAILABLE_TEXT,
                None,
                follow_at,
            )
        elif chosen_url is not None:
            assert desktop is not None
            enqueue_open(
                account_id=opened.account["id"],
                computer_id=desktop["computer_id"],
                url=chosen_url,
                bot_id=parsed_id,
                chat_id=chat["id"],
                login=text,
            )
            follow_at = opened.now
        elif (
            desktop is not None
            and address is None
            and bot_holds_browser(
                opened.account["id"], desktop["computer_id"], parsed_id
            )
        ):
            hold_text(opened.account["id"], parsed_id, text)
            follow_at = opened.now
        elif address is not None:
            assert desktop is not None
            enqueue_open(
                account_id=opened.account["id"],
                computer_id=desktop["computer_id"],
                url=address,
                bot_id=parsed_id,
                chat_id=chat["id"],
            )
            follow_at = opened.now
        elif desktop is not None and not desktop_available:
            _insert_message(
                opened.connection,
                chat["id"],
                "error",
                COMPUTER_UNAVAILABLE_TEXT,
                None,
                follow_at,
            )
        else:
            reply = aggregator.complete(
                description=bot["description"],
                scenario=bot["scenario"],
                messages=_adapter_messages(opened.connection, chat["id"]),
                desktop_available=desktop_available,
            )
            if reply == "":
                _insert_message(
                    opened.connection,
                    chat["id"],
                    "error",
                    EMPTY_MODEL_TEXT,
                    EMPTY_MODEL_CODE,
                    follow_at,
                )
            else:
                _insert_message(
                    opened.connection,
                    chat["id"],
                    "assistant",
                    reply,
                    None,
                    follow_at,
                )
                opened.connection.execute(
                    quota_charges.insert().values(
                        id=uuid.uuid4(),
                        period_id=counter["period_id"],
                        created_at=opened.now,
                    )
                )
                opened.connection.execute(
                    quota_counters.update()
                    .where(quota_counters.c.id == counter["id"])
                    .values(used=counter["used"] + 1)
                )
        opened.connection.execute(
            chats.update()
            .where(chats.c.id == chat["id"])
            .values(updated_at=follow_at)
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(_messages_document(opened.connection, chat["id"]))


@router.get("/api/bots/{bot_id}/chats")
def list_chats(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        rows = _bot_chats(opened.connection, parsed_id)
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse({"chats": [chat_document(row) for row in rows]})


@router.post("/api/bots/{bot_id}/chats")
def create_extra_chat(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        chat_id = uuid.uuid4()
        opened.connection.execute(
            chats.insert().values(
                id=chat_id,
                bot_id=parsed_id,
                updated_at=opened.now,
                kind=EXTRA_KIND,
                created_at=opened.now,
            )
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        row = _owned_chat(opened.connection, chat_id, parsed_id)
        assert row is not None
        return JSONResponse(chat_document(row))


@router.get("/api/bots/{bot_id}/chats/{chat_id}/messages")
def list_chat_messages(request: Request, bot_id: str, chat_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_chat = parse_id(chat_id)
        if parsed_chat is None:
            return chat_not_found()
        chat = _owned_chat(opened.connection, parsed_chat, parsed_id)
        if chat is None:
            return chat_not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(_messages_document(opened.connection, chat["id"]))
