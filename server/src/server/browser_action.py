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
from server.library import attach_files, store_library_file
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
SUBSCRIPTION_INACTIVE_TEXT = "Подписка неактивна."

_lock = threading.Lock()
_opens: dict[tuple[uuid.UUID, uuid.UUID], list["_Open"]] = {}
_choices: dict[tuple[uuid.UUID, uuid.UUID], "_Pending"] = {}
_held: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = {}
_SAVED_FACT = "Доступ сохранён."


class _Open:
    def __init__(
        self,
        url: str,
        bot_id: uuid.UUID,
        chat_id: uuid.UUID,
        login: str | None = None,
    ) -> None:
        self.url = url
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.login = login


class _Pending:
    def __init__(
        self,
        url: str,
        site: str,
        logins: tuple[str, ...],
        bot_id: uuid.UUID,
        chat_id: uuid.UUID,
        kind: str,
    ) -> None:
        self.url = url
        self.site = site
        self.logins = logins
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.kind = kind


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
    login: str | None = None,
) -> None:
    with _lock:
        if login is None:
            _choices.pop((account_id, computer_id), None)
        _opens.setdefault((account_id, computer_id), []).append(
            _Open(url, bot_id, chat_id, login)
        )


def peek_open(account_id: uuid.UUID, computer_id: uuid.UUID) -> str | None:
    item = _first_open(account_id, computer_id)
    if item is None:
        return None
    return item.url


def _first_open(account_id: uuid.UUID, computer_id: uuid.UUID) -> _Open | None:
    with _lock:
        items = _opens.get((account_id, computer_id))
        if not items:
            return None
        return items[0]


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
        _choices.clear()
        _held.clear()


def forget_account(account_id: uuid.UUID) -> None:
    with _lock:
        for store in (_opens, _choices, _held):
            for key in [key for key in store if key[0] == account_id]:
                del store[key]


def bot_holds_browser(
    account_id: uuid.UUID, computer_id: uuid.UUID, bot_id: uuid.UUID
) -> bool:
    item = _first_open(account_id, computer_id)
    return item is not None and item.bot_id == bot_id


def hold_text(
    account_id: uuid.UUID, bot_id: uuid.UUID, chat_id: uuid.UUID, text: str
) -> None:
    with _lock:
        _held.setdefault((account_id, bot_id), []).append((chat_id, text))


def take_login_choice(
    *,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    bot_id: uuid.UUID,
    text: str,
) -> str | None:
    with _lock:
        pending = _choices.get((account_id, computer_id))
        if pending is None or pending.kind != "choose" or pending.bot_id != bot_id:
            return None
        if text not in pending.logins:
            return None
        del _choices[(account_id, computer_id)]
        return pending.url


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


def _may_finish_browser(
    connection: Connection, account_id: uuid.UUID, computer_id: uuid.UUID
) -> bool:
    if _working(connection, account_id, computer_id):
        return True
    return peek_open(account_id, computer_id) is not None


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


class _Report:
    def __init__(self, kind: str, text: str, site: str, choices: tuple[str, ...]) -> None:
        self.kind = kind
        self.text = text
        self.site = site
        self.choices = choices


def _page_text_ok(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > _PAGE_TEXT_LIMIT:
        return None
    return value


def _site_ok(value: object) -> str | None:
    if not isinstance(value, str) or value == "" or len(value) > 253 or "\x00" in value:
        return None
    return value


def _choices_ok(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or value == [] or len(value) > 20:
        return None
    choices: list[str] = []
    for item in value:
        if not isinstance(item, str) or item == "" or len(item) > 200 or "\x00" in item:
            return None
        choices.append(item)
    return tuple(choices)


async def _read_browser_report(request: Request) -> _Report | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", _PAGE_TEXT)
    if not isinstance(body, dict) or "password" in body or "cookie" in body:
        return error_response(400, "invalid_request", _PAGE_TEXT)
    keys = set(body)
    if keys == {"text"} or (
        keys == {"text", "login"} and body.get("login") in {"session", "entered"}
    ):
        text = _page_text_ok(body.get("text"))
        if text is None:
            return error_response(400, "invalid_request", _PAGE_TEXT)
        return _Report("page", text, "", ())
    if keys == {"text", "login", "site", "choices"} and body.get("login") == "choose":
        text = _page_text_ok(body.get("text"))
        site = _site_ok(body.get("site"))
        choices = _choices_ok(body.get("choices"))
        if text is None or site is None or choices is None:
            return error_response(400, "invalid_request", _PAGE_TEXT)
        return _Report("choose", text, site, choices)
    if keys == {"text", "login", "site"} and body.get("login") == "form":
        text = _page_text_ok(body.get("text"))
        site = _site_ok(body.get("site"))
        if text is None or site is None:
            return error_response(400, "invalid_request", _PAGE_TEXT)
        return _Report("form", text, site, ())
    if keys == {"login", "site"} and body.get("login") == "saved":
        site = _site_ok(body.get("site"))
        if site is None:
            return error_response(400, "invalid_request", _PAGE_TEXT)
        return _Report("saved", "", site, ())
    return error_response(400, "invalid_request", _PAGE_TEXT)


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
    payload: list[dict[str, object]] = [
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
    return {"messages": payload}


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


def _period_open(connection: Connection, account_id: uuid.UUID, now: datetime) -> bool:
    from server.routines import period_is_active

    return period_is_active(connection, account_id, now)


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
    if not _period_open(connection, account_id, now):
        _insert_message(
            connection,
            pending.chat_id,
            "error",
            SUBSCRIPTION_INACTIVE_TEXT,
            None,
            reply_at,
        )
    else:
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
            text, xlsx = aggregator.split_reply(reply)
            if text == "":
                _insert_message(
                    connection,
                    pending.chat_id,
                    "error",
                    _EMPTY_MODEL_TEXT,
                    _EMPTY_MODEL_CODE,
                    reply_at,
                )
            else:
                message_id = _insert_message(
                    connection,
                    pending.chat_id,
                    "assistant",
                    text,
                    None,
                    reply_at,
                )
                if xlsx is not None:
                    store_library_file(
                        connection,
                        bot_id=bot["id"],
                        chat_id=pending.chat_id,
                        message_id=message_id,
                        name=xlsx[0],
                        content=xlsx[1],
                        created_at=reply_at,
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
    release_held(connection, account_id, computer_id, pending.bot_id, reply_at)
    return JSONResponse(_messages_document(connection, pending.chat_id))


def release_held(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    bot_id: uuid.UUID,
    now: datetime,
) -> None:
    if bot_holds_browser(account_id, computer_id, bot_id):
        return
    with _lock:
        waiting = _held.pop((account_id, bot_id), [])
    for chat_id, _text in waiting:
        _answer_held(connection, account_id, bot_id, chat_id, now)


def _answer_held(
    connection: Connection,
    account_id: uuid.UUID,
    bot_id: uuid.UUID,
    chat_id: uuid.UUID,
    now: datetime,
) -> None:
    bot = (
        connection.execute(
            select(bots).where(
                bots.c.id == bot_id,
                bots.c.account_id == account_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if bot is None:
        return
    chat_id = connection.execute(
        select(chats.c.id).where(
            chats.c.id == chat_id,
            chats.c.bot_id == bot_id,
        )
    ).scalar_one_or_none()
    if chat_id is None:
        return
    reply_at = _reply_at(connection, chat_id, now)
    if not _period_open(connection, account_id, now):
        _insert_message(
            connection,
            chat_id,
            "error",
            SUBSCRIPTION_INACTIVE_TEXT,
            None,
            reply_at,
        )
    else:
        counter = _lock_quota(connection, account_id)
        if counter is None or counter["used"] >= development_tariff.model_requests:
            _insert_message(
                connection,
                chat_id,
                "error",
                _QUOTA_EXHAUSTED_TEXT,
                None,
                reply_at,
            )
        else:
            reply = aggregator.complete(
                description=bot["description"],
                scenario=bot["scenario"],
                messages=_adapter_messages(connection, chat_id),
                desktop_available=_pulse_live(connection, account_id),
            )
            text, xlsx = aggregator.split_reply(reply)
            if text == "":
                _insert_message(
                    connection,
                    chat_id,
                    "error",
                    _EMPTY_MODEL_TEXT,
                    _EMPTY_MODEL_CODE,
                    reply_at,
                )
            else:
                message_id = _insert_message(
                    connection,
                    chat_id,
                    "assistant",
                    text,
                    None,
                    reply_at,
                )
                if xlsx is not None:
                    store_library_file(
                        connection,
                        bot_id=bot_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        name=xlsx[0],
                        content=xlsx[1],
                        created_at=reply_at,
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
        chats.update().where(chats.c.id == chat_id).values(updated_at=reply_at)
    )


def _write_assistant(
    connection: Connection,
    account_id: uuid.UUID,
    bot_id: uuid.UUID,
    chat_id: uuid.UUID,
    now: datetime,
    text: str,
) -> JSONResponse:
    bot = (
        connection.execute(
            select(bots).where(
                bots.c.id == bot_id,
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
                chats.c.id == chat_id,
                chats.c.bot_id == bot["id"],
            )
        )
        .scalar_one_or_none()
    )
    if chat is None:
        return _bot_not_found()
    reply_at = _reply_at(connection, chat_id, now)
    _insert_message(connection, chat_id, "assistant", text, None, reply_at)
    connection.execute(
        chats.update().where(chats.c.id == chat_id).values(updated_at=reply_at)
    )
    return JSONResponse(_messages_document(connection, chat_id))


def _same_host(url: str, site: str) -> bool:
    return urlsplit(url).hostname == site


def _remember(
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    pending: _Pending,
) -> None:
    with _lock:
        _choices[(account_id, computer_id)] = pending


def finish_choose(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    now: datetime,
    site: str,
    choices: tuple[str, ...],
) -> JSONResponse | Response:
    pending = _first_open(account_id, computer_id)
    if pending is None:
        return Response(status_code=204)
    if not _same_host(pending.url, site):
        return error_response(400, "invalid_request", _PAGE_TEXT)
    pop_open(account_id, computer_id)
    question = f"Какую запись использовать для {site}? {' или '.join(choices)}."
    _remember(
        account_id,
        computer_id,
        _Pending(pending.url, site, choices, pending.bot_id, pending.chat_id, "choose"),
    )
    document = _write_assistant(
        connection,
        account_id,
        pending.bot_id,
        pending.chat_id,
        now,
        question,
    )
    release_held(connection, account_id, computer_id, pending.bot_id, now)
    return document


def finish_form(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    now: datetime,
    site: str,
) -> JSONResponse | Response:
    pending = _first_open(account_id, computer_id)
    if pending is None:
        return Response(status_code=204)
    if not _same_host(pending.url, site):
        return error_response(400, "invalid_request", _PAGE_TEXT)
    pop_open(account_id, computer_id)
    sentence = f"Сайт {site} просит вход. Записи для него нет."
    _remember(
        account_id,
        computer_id,
        _Pending(pending.url, site, (), pending.bot_id, pending.chat_id, "form"),
    )
    document = _write_assistant(
        connection,
        account_id,
        pending.bot_id,
        pending.chat_id,
        now,
        sentence,
    )
    release_held(connection, account_id, computer_id, pending.bot_id, now)
    return document


def finish_saved(
    connection: Connection,
    account_id: uuid.UUID,
    computer_id: uuid.UUID,
    now: datetime,
    site: str,
) -> JSONResponse | Response:
    with _lock:
        pending = _choices.get((account_id, computer_id))
        if pending is None or pending.kind != "form" or pending.site != site:
            return Response(status_code=204)
        del _choices[(account_id, computer_id)]
    document = _write_assistant(
        connection,
        account_id,
        pending.bot_id,
        pending.chat_id,
        now,
        _SAVED_FACT,
    )
    release_held(connection, account_id, computer_id, pending.bot_id, now)
    return document


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
        if parsed is None or not _may_finish_browser(connection, account["id"], parsed):
            return _desktop_not_found()
        item = _first_open(account["id"], parsed)
        touch_session(connection, session["id"], now)
    if item is None:
        return Response(status_code=204)
    payload = {"url": item.url}
    if item.login:
        payload["login"] = item.login
    return JSONResponse(payload)


@router.post("/api/desktops/{computer_id}/browser-task")
async def post_browser_task(request: Request, computer_id: str) -> Response:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    report = await _read_browser_report(request)
    if isinstance(report, JSONResponse):
        return report
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            return unauthenticated()
        session, account = current
        if account["email_confirmed_at"] is None:
            return _email_unconfirmed()
        parsed = _parse_computer_id(computer_id)
        if parsed is None or not _may_finish_browser(connection, account["id"], parsed):
            return _desktop_not_found()
        if report.kind == "page":
            document = finish_open(connection, account["id"], parsed, now, report.text)
        elif report.kind == "choose":
            document = finish_choose(
                connection, account["id"], parsed, now, report.site, report.choices
            )
        elif report.kind == "form":
            document = finish_form(connection, account["id"], parsed, now, report.site)
        else:
            document = finish_saved(connection, account["id"], parsed, now, report.site)
        touch_session(connection, session["id"], now)
    return document
