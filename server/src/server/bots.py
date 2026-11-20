import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

import server.aggregator as aggregator
from server.accounts import find_account_by_id, format_utc, touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.browser_action import (
    SUBSCRIPTION_INACTIVE_TEXT,
    bot_holds_browser,
    enqueue_open,
    hold_text,
    named_address,
    take_login_choice,
)
from server.chief import ASSIGNMENT_CODE
from server.config import development_tariff
from server.dangerous import DANGEROUS_CODE, gate_state
from server.desktops import find_working_desktop
from server.library import attach_files, store_library_file
from server.models import (
    bots,
    chats,
    dangerous_steps,
    desktop_notices,
    library_files,
    messages,
    quota_charges,
    quota_counters,
    routine_checks,
    routines,
    subscription_periods,
)
from server.scenario_chat import PROPOSAL_CODE, confirm_proposal, proposed_scenario

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
PAYLOAD_TOO_LARGE = "Сообщение длиннее 8 000 символов."
INVALID_BODY = "Тело запроса не текст."
WEBHOOK_UNKNOWN = "Адрес не найден."
EMPTY_MODEL_TEXT = "Ответ модели не пришёл."
EMPTY_MODEL_CODE = "empty_model_response"
QUOTA_EXHAUSTED_TEXT = "Квота на период закончилась."
COMPUTER_UNAVAILABLE_TEXT = "компьютер недоступен"
CHAT_NOT_FOUND = "Чат не найден."
FILE_NOT_FOUND = "Файл не найден."
SPREADSHEET_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
MAIN_CHAT_KEPT = "Основной чат удалить нельзя."
PROPOSAL_NOT_FOUND = "Сообщение не найдено."
INVALID_CONFIRM = "Нужно подтверждение."
INVALID_CHIEF = "Нужен признак главного."
NOT_CHIEF = "Этот бот не главный."
ASSIGN_OTHER = "Поручение уходит другому боту."
MAIN_KIND = "main"
EXTRA_KIND = "extra"


class Opened(NamedTuple):
    connection: Connection
    session: RowMapping | None
    account: RowMapping
    now: datetime


def email_unconfirmed() -> JSONResponse:
    return error_response(403, "email_unconfirmed", "Почта ещё не подтверждена.")


def webhook_unknown() -> JSONResponse:
    return error_response(404, "webhook_unknown", WEBHOOK_UNKNOWN)


def not_found() -> JSONResponse:
    return error_response(404, "not_found", NOT_FOUND)


def chat_not_found() -> JSONResponse:
    return error_response(404, "not_found", CHAT_NOT_FOUND)


def file_not_found() -> JSONResponse:
    return error_response(404, "not_found", FILE_NOT_FOUND)


def proposal_not_found() -> JSONResponse:
    return error_response(404, "not_found", PROPOSAL_NOT_FOUND)


def invalid_confirm() -> JSONResponse:
    return error_response(400, "invalid_request", INVALID_CONFIRM)


def main_chat_kept() -> JSONResponse:
    return error_response(400, "main_chat", MAIN_CHAT_KEPT)


def new_inbound_secret() -> str:
    return secrets.token_urlsafe(32)


def inbound_webhook_address(secret: str) -> str:
    return f"/api/hooks/{secret}"


def hook_message(method: str, query: str, body: str) -> str:
    lines = [method]
    if query != "":
        lines.append(query)
    if body != "":
        lines.append(body)
    return "\n".join(lines)


def _decoded_hook_body(raw: bytes) -> str | JSONResponse:
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return error_response(400, "invalid_body", INVALID_BODY)
    if len(body) > MESSAGE_LIMIT:
        return error_response(400, "payload_too_large", PAYLOAD_TOO_LARGE)
    return body


class _DropHookAccess(logging.Filter):
    # The access log line includes the request path, which is the secret address.
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/hooks/" not in record.getMessage()


def _install_hook_log_filter() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if any(isinstance(item, _DropHookAccess) for item in logger.filters):
            continue
        logger.addFilter(_DropHookAccess())


_install_hook_log_filter()


def bot_document(row: RowMapping) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "color": row["color"],
        "icon": row["icon"],
        "description": row["description"],
        "scenario": row["scenario"],
        "updated_at": format_utc(row["updated_at"]),
        "chief": row["chief"] is True,
        "inbound_webhook": inbound_webhook_address(row["inbound_secret"]),
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


def parse_patch(body: dict[str, object]) -> dict[str, object] | JSONResponse:
    changes: dict[str, object] = {}
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
    if "chief" in body:
        if not isinstance(body["chief"], bool):
            return error_response(400, "invalid_request", INVALID_CHIEF)
        changes["chief"] = body["chief"]
    return changes


def _remove_bot(connection: Connection, bot_id: uuid.UUID) -> None:
    chat_ids = select(chats.c.id).where(chats.c.bot_id == bot_id)
    connection.execute(
        dangerous_steps.delete().where(dangerous_steps.c.bot_id == bot_id)
    )
    connection.execute(
        desktop_notices.delete().where(desktop_notices.c.bot_id == bot_id)
    )
    connection.execute(
        routine_checks.delete().where(routine_checks.c.bot_id == bot_id)
    )
    connection.execute(routines.delete().where(routines.c.bot_id == bot_id))
    connection.execute(messages.delete().where(messages.c.chat_id.in_(chat_ids)))
    connection.execute(chats.delete().where(chats.c.bot_id == bot_id))
    connection.execute(bots.delete().where(bots.c.id == bot_id))


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
) -> uuid.UUID:
    message_id = uuid.uuid4()
    connection.execute(
        messages.insert().values(
            id=message_id,
            chat_id=chat_id,
            role=role,
            text=text,
            code=code,
            updated_at=updated_at,
        )
    )
    return message_id


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
    attach_files(connection, payload, [row["id"] for row in rows])
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
                inbound_secret=new_inbound_secret(),
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
        chief = changes.pop("chief", None)
        if chief is True:
            opened.connection.execute(
                bots.update()
                .where(
                    bots.c.account_id == opened.account["id"],
                    bots.c.id != parsed_id,
                    bots.c.chief.is_(True),
                )
                .values(chief=False, updated_at=opened.now)
            )
        values = dict(changes)
        if isinstance(chief, bool):
            values["chief"] = chief
        opened.connection.execute(
            bots.update()
            .where(
                bots.c.id == parsed_id,
                bots.c.account_id == opened.account["id"],
            )
            .values(**values, updated_at=opened.now)
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        row = _load_bot(opened.connection, parsed_id, opened.account["id"])
        assert row is not None
        return JSONResponse(bot_document(row))


@router.delete("/api/bots/{bot_id}")
def delete_bot(request: Request, bot_id: str) -> Response:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        _remove_bot(opened.connection, parsed_id)
        return Response(status_code=204)


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


def _touch_turn(opened: Opened) -> None:
    if opened.session is None:
        return
    touch_session(opened.connection, opened.session["id"], opened.now)


def _deliver_message(
    opened: Opened,
    bot: RowMapping,
    chat: RowMapping,
    text: str,
    user_code: str | None = None,
) -> JSONResponse:
    _insert_message(
        opened.connection,
        chat["id"],
        "user",
        text,
        user_code,
        opened.now,
    )
    follow_at = opened.now + timedelta(microseconds=1)
    from server.routines import period_is_active

    if not period_is_active(opened.connection, opened.account["id"], opened.now):
        _insert_message(
            opened.connection,
            chat["id"],
            "error",
            SUBSCRIPTION_INACTIVE_TEXT,
            None,
            follow_at,
        )
        opened.connection.execute(
            chats.update().where(chats.c.id == chat["id"]).values(updated_at=follow_at)
        )
        _touch_turn(opened)
        return JSONResponse(_messages_document(opened.connection, chat["id"]))
    counter = _lock_quota_counter(opened.connection, opened.account["id"])
    proposed = proposed_scenario(text)
    if proposed is not None:
        _insert_message(
            opened.connection,
            chat["id"],
            "assistant",
            proposed,
            PROPOSAL_CODE,
            follow_at,
        )
        opened.connection.execute(
            chats.update().where(chats.c.id == chat["id"]).values(updated_at=follow_at)
        )
        _touch_turn(opened)
        return JSONResponse(_messages_document(opened.connection, chat["id"]))
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
            bot_id=bot["id"],
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
            bot_id=bot["id"],
            chat_id=chat["id"],
            login=text,
        )
        follow_at = opened.now
    elif (
        desktop is not None
        and address is None
        and bot_holds_browser(
            opened.account["id"], desktop["computer_id"], bot["id"]
        )
    ):
        hold_text(opened.account["id"], bot["id"], chat["id"], text)
        follow_at = opened.now
    elif address is not None:
        assert desktop is not None
        enqueue_open(
            account_id=opened.account["id"],
            computer_id=desktop["computer_id"],
            url=address,
            bot_id=bot["id"],
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
            page_text="",
        )
        reply_text, xlsx = aggregator.split_reply(reply)
        if reply_text == "":
            _insert_message(
                opened.connection,
                chat["id"],
                "error",
                EMPTY_MODEL_TEXT,
                EMPTY_MODEL_CODE,
                follow_at,
            )
        else:
            message_id = _insert_message(
                opened.connection,
                chat["id"],
                "assistant",
                reply_text,
                None,
                follow_at,
            )
            if xlsx is not None:
                store_library_file(
                    opened.connection,
                    bot_id=bot["id"],
                    chat_id=chat["id"],
                    message_id=message_id,
                    name=xlsx[0],
                    content=xlsx[1],
                    created_at=follow_at,
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
        chats.update().where(chats.c.id == chat["id"]).values(updated_at=follow_at)
    )
    _touch_turn(opened)
    return JSONResponse(_messages_document(opened.connection, chat["id"]))


def _raw_query(request: Request) -> str:
    raw = request.scope.get("query_string", b"")
    if isinstance(raw, str):
        return raw
    return raw.decode("latin-1")


@router.api_route("/api/hooks/{token}", methods=["GET", "POST"])
async def receive_hook(request: Request, token: str) -> Response:
    _install_hook_log_filter()
    query = _raw_query(request)
    raw = await request.body()
    now = utc_now()
    with begin() as connection:
        bot = (
            connection.execute(select(bots).where(bots.c.inbound_secret == token))
            .mappings()
            .one_or_none()
        )
        if bot is None:
            return webhook_unknown()
        account = find_account_by_id(connection, bot["account_id"])
        if account is None:
            return webhook_unknown()
        if account["email_confirmed_at"] is None:
            return email_unconfirmed()
        body = _decoded_hook_body(raw)
        if isinstance(body, JSONResponse):
            return body
        _deliver_message(
            Opened(connection, None, account, now),
            bot,
            main_chat(connection, bot["id"]),
            hook_message(request.method, query, body),
        )
    return JSONResponse({"ok": True})


def _not_chief() -> JSONResponse:
    return error_response(400, "not_chief", NOT_CHIEF)


def _assign_other() -> JSONResponse:
    return error_response(400, "invalid_request", ASSIGN_OTHER)


@router.post("/api/bots/{bot_id}/assignments")
async def create_assignment(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        chief = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if chief is None:
            return not_found()
        if chief["chief"] is not True:
            return _not_chief()
        body = await read_object(request)
        if isinstance(body, JSONResponse):
            return body
        if "password" in body or "cookie" in body:
            return error_response(400, "invalid_message", INVALID_MESSAGE)
        target_raw = body.get("bot_id")
        if not isinstance(target_raw, str):
            return not_found()
        target_id = parse_id(target_raw)
        if target_id is None:
            return not_found()
        if target_id == parsed_id:
            return _assign_other()
        target = _load_bot(opened.connection, target_id, opened.account["id"])
        if target is None:
            return not_found()
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= MESSAGE_LIMIT:
            return error_response(400, "invalid_message", INVALID_MESSAGE)
        return _deliver_message(
            opened,
            target,
            main_chat(opened.connection, target_id),
            text,
            ASSIGNMENT_CODE,
        )


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
        return _deliver_message(
            opened, bot, main_chat(opened.connection, parsed_id), text
        )


@router.post("/api/bots/{bot_id}/chats/{chat_id}/messages")
async def create_chat_message(
    request: Request, bot_id: str, chat_id: str
) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
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
        text = await read_message_text(request)
        if isinstance(text, JSONResponse):
            return text
        return _deliver_message(opened, bot, chat, text)


async def read_confirm(request: Request) -> bool | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return invalid_confirm()
    if (
        not isinstance(body, dict)
        or "password" in body
        or "cookie" in body
        or body.get("confirm") is not True
    ):
        return invalid_confirm()
    return True


@router.post("/api/bots/{bot_id}/messages/{message_id}/scenario")
async def confirm_scenario(
    request: Request, bot_id: str, message_id: str
) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_message = parse_id(message_id)
        if parsed_message is None:
            return proposal_not_found()
        confirmed = await read_confirm(request)
        if isinstance(confirmed, JSONResponse):
            return confirmed
        outcome, chat_id = confirm_proposal(
            opened.connection,
            parsed_id,
            parsed_message,
            opened.now,
        )
        if outcome == "missing" or chat_id is None:
            return proposal_not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(_messages_document(opened.connection, chat_id))


@router.delete("/api/bots/{bot_id}/chats/{chat_id}")
def delete_chat(request: Request, bot_id: str, chat_id: str) -> Response:
    with open_confirmed(request, mutating=True) as opened:
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
        if chat["kind"] == MAIN_KIND:
            return main_chat_kept()
        opened.connection.execute(
            messages.delete().where(messages.c.chat_id == chat["id"])
        )
        opened.connection.execute(chats.delete().where(chats.c.id == chat["id"]))
        touch_session(opened.connection, opened.session["id"], opened.now)
        return Response(status_code=204)


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


def _library_document(row: RowMapping) -> dict[str, str]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "created_at": format_utc(row["created_at"]),
    }


def _library_rows(connection: Connection, bot_id: uuid.UUID) -> list[RowMapping]:
    return (
        connection.execute(
            select(
                library_files.c.id,
                library_files.c.name,
                library_files.c.created_at,
            )
            .where(library_files.c.bot_id == bot_id)
            .order_by(library_files.c.created_at.asc(), library_files.c.id.asc())
        )
        .mappings()
        .all()
    )


def _library_file(
    connection: Connection, bot_id: uuid.UUID, file_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(library_files).where(
                library_files.c.id == file_id,
                library_files.c.bot_id == bot_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def _spreadsheet_disposition(name: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/api/bots/{bot_id}/library")
def list_library(request: Request, bot_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        rows = _library_rows(opened.connection, parsed_id)
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse({"files": [_library_document(row) for row in rows]})


@router.get("/api/bots/{bot_id}/library/{file_id}")
def read_library_file(request: Request, bot_id: str, file_id: str) -> Response:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_file = parse_id(file_id)
        if parsed_file is None:
            return file_not_found()
        row = _library_file(opened.connection, parsed_id, parsed_file)
        if row is None:
            return file_not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        return Response(
            content=bytes(row["content"]),
            media_type=SPREADSHEET_TYPE,
            headers={"Content-Disposition": _spreadsheet_disposition(row["name"])},
        )


@router.delete("/api/bots/{bot_id}/library/{file_id}")
def delete_library_file(request: Request, bot_id: str, file_id: str) -> Response:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(bot_id)
        if parsed_id is None:
            return not_found()
        bot = _load_bot(opened.connection, parsed_id, opened.account["id"])
        if bot is None:
            return not_found()
        parsed_file = parse_id(file_id)
        if parsed_file is None:
            return file_not_found()
        deleted = opened.connection.execute(
            library_files.delete().where(
                library_files.c.id == parsed_file,
                library_files.c.bot_id == parsed_id,
            )
        )
        if deleted.rowcount == 0:
            return file_not_found()
        touch_session(opened.connection, opened.session["id"], opened.now)
        return Response(status_code=204)
