import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

import server.aggregator as aggregator
from server.accounts import format_utc, touch_session, utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)
from server.config import development_tariff
from server.models import (
    bots,
    chats,
    desktops,
    messages,
    quota_charges,
    quota_counters,
    subscription_periods,
)

router = APIRouter()

_ADDRESS = re.compile(r"https?://[^\s<>'\"]+")
_PAGE_TEXT = "Нужен текст страницы."
_PAGE_TEXT_LIMIT = 200_000
_NOT_FOUND_DESKTOP = "Десктоп не найден."
_NOT_FOUND_BOT = "Бот не найден."
_EMPTY_MODEL_TEXT = "Ответ модели не пришёл."
_EMPTY_MODEL_CODE = "empty_model_response"
_QUOTA_EXHAUSTED_TEXT = "Квота на период закончилась."

_lock = threading.Lock()
_opens: dict[tuple[uuid.UUID, uuid.UUID], list["_Open"]] = {}


class _Open:
    def __init__(self, url: str, bot_id: uuid.UUID, chat_id: uuid.UUID) -> None:
        self.url = url
        self.bot_id = bot_id
        self.chat_id = chat_id


def named_address(text: str) -> str | None:
    match = _ADDRESS.search(text)
    if match is None:
        return None
    raw = match.group(0).rstrip(".,;:)]}")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or parts.hostname is None:
        return None
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def enqueue_open(
    *,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    url: str,
    bot_id: uuid.UUID,
    chat_id: uuid.UUID,
) -> None:
    with _lock:
        _opens.setdefault((account_id, computer_id), []).append(
            _Open(url, bot_id, chat_id)
        )


def peek_open(account_id: uuid.UUID, computer_id: uuid.UUID) -> str | None:
    with _lock:
        items = _opens.get((account_id, computer_id))
        if not items:
            return None
        return items[0].url


def pop_open(account_id: uuid.UUID, computer_id: uuid.UUID) -> _Open | None:
    with _lock:
        items = _opens.get((account_id, computer_id))
        if not items:
            return None
        item = items.pop(0)
        if not items:
            del _opens[(account_id, computer_id)]
        return item


def clear_opens() -> None:
    with _lock:
        _opens.clear()


def _parse_computer_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _desktop_not_found() -> JSONResponse:
    return error_response(404, "not_found", _NOT_FOUND_DESKTOP)


def _bot_not_found() -> JSONResponse:
    return error_response(404, "not_found", _NOT_FOUND_BOT)


def _email_unconfirmed() -> JSONResponse:
    return error_response(403, "email_unconfirmed", "Почта ещё не подтверждена.")


def _working(
    connection: Connection, account_id: uuid.UUID, computer_id: uuid.UUID
) -> bool:
    row = (
        connection.execute(
            select(desktops.c.id).where(
                desktops.c.account_id == account_id,
                desktops.c.computer_id == computer_id,
                desktops.c.working.is_(True),
            )
        )
        .scalars()
        .one_or_none()
    )
    return row is not None


def _pulse_live(connection: Connection, account_id: uuid.UUID) -> bool:
    pulsed = (
        connection.execute(
            select(desktops.c.last_pulse_at).where(
                desktops.c.account_id == account_id,
                desktops.c.working.is_(True),
            )
        )
        .scalar_one_or_none()
    )
    return pulsed is not None


async def _read_page_text(request: Request) -> str | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", _PAGE_TEXT)
    if not isinstance(body, dict) or set(body) != {"text"}:
        return error_response(400, "invalid_request", _PAGE_TEXT)
    text = body.get("text")
    if not isinstance(text, str) or len(text) > _PAGE_TEXT_LIMIT:
        return error_response(400, "invalid_request", _PAGE_TEXT)
    return text


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
    return {
        "messages": [
            {
                "id": str(row["id"]),
                "role": row["role"],
                "text": row["text"],
                "code": row["code"],
                "updated_at": format_utc(row["updated_at"]),
            }
            for row in rows
        ]
    }


def _lock_quota(connection: Connection, account_id: uuid.UUID):
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
        return None
    return (
        connection.execute(
            select(quota_counters)
            .where(quota_counters.c.period_id == period)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )


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


def _reply_at(connection: Connection, chat_id: uuid.UUID, now: datetime) -> datetime:
    latest = connection.execute(
        select(func.max(messages.c.updated_at)).where(messages.c.chat_id == chat_id)
    ).scalar_one()
    if latest is not None and now <= latest:
        return latest + timedelta(microseconds=1)
    return now


def finish_open(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    now: datetime,
    page_text: str,
) -> JSONResponse | Response:
    if not isinstance(page_text, str):
        return error_response(400, "invalid_request", _PAGE_TEXT)
    pending = pop_open(account_id, computer_id)
    if pending is None:
        return Response(status_code=204)
    bot = (
        connection.execute(
            select(bots).where(
                bots.c.id == pending.bot_id,
                bots.c.account_id == account_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if bot is None:
        return _bot_not_found()
    chat = (
        connection.execute(
            select(chats.c.id).where(
                chats.c.id == pending.chat_id,
                chats.c.bot_id == bot["id"],
            )
        )
        .scalar_one_or_none()
    )
    if chat is None:
        return _bot_not_found()
    reply_at = _reply_at(connection, pending.chat_id, now)
    counter = _lock_quota(connection, account_id)
    if counter is None or counter["used"] >= development_tariff.model_requests:
        _insert_message(
            connection,
            pending.chat_id,
            "error",
            _QUOTA_EXHAUSTED_TEXT,
            None,
            reply_at,
        )
    else:
        reply = aggregator.complete(
            description=bot["description"],
            scenario=bot["scenario"],
            messages=_adapter_messages(connection, pending.chat_id),
            desktop_available=_pulse_live(connection, account_id),
        )
        if reply == "":
            _insert_message(
                connection,
                pending.chat_id,
                "error",
                _EMPTY_MODEL_TEXT,
                _EMPTY_MODEL_CODE,
                reply_at,
            )
        else:
            _insert_message(
                connection,
                pending.chat_id,
                "assistant",
                reply,
                None,
                reply_at,
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
        chats.update().where(chats.c.id == pending.chat_id).values(updated_at=reply_at)
    )
    return JSONResponse(_messages_document(connection, pending.chat_id))


@router.get("/api/desktops/{computer_id}/browser-task")
def get_browser_task(request: Request, computer_id: str) -> Response:
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        session, account = current
        if account["email_confirmed_at"] is None:
            return _email_unconfirmed()
        parsed = _parse_computer_id(computer_id)
        if parsed is None or not _working(connection, account["id"], parsed):
            return _desktop_not_found()
        url = peek_open(account["id"], parsed)
        touch_session(connection, session["id"], now)
    if url is None:
        return Response(status_code=204)
    return JSONResponse({"url": url})


@router.post("/api/desktops/{computer_id}/browser-task")
async def post_browser_task(request: Request, computer_id: str) -> Response:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    page_text = await _read_page_text(request)
    if isinstance(page_text, JSONResponse):
        return page_text
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        session, account = current
        if account["email_confirmed_at"] is None:
            return _email_unconfirmed()
        parsed = _parse_computer_id(computer_id)
        if parsed is None or not _working(connection, account["id"], parsed):
            return _desktop_not_found()
        document = finish_open(connection, account["id"], parsed, now, page_text)
        touch_session(connection, session["id"], now)
    return document
