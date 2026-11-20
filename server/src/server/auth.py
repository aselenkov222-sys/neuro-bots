import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from server.accounts import (
    EMAIL_MAX_LENGTH,
    EmailTooLong,
    ProfileNameRejected,
    calendar_month_end,
    create_account,
    create_session,
    delete_account,
    delete_session,
    find_account_by_email,
    find_account_by_id,
    find_session,
    format_utc,
    normalize_email,
    session_is_current,
    touch_session,
    update_account_name,
    utc_now,
)
from server.config import (
    configured_tariff_name,
    configured_tariff_price,
    development_tariff,
    payment_required,
)
from server.confirmation import (
    apply_confirmation,
    issue_confirmation_link,
    log_confirmation_link,
    replace_confirmation_link,
)
from server.db import create_db_engine
from server.models import quota_counters, subscription_periods
from server.passwords import PasswordNotAccepted, password_accepted, verify_password

router = APIRouter()
_CABINET_HTML = Path(__file__).resolve().parents[3] / "web" / "index.html"


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def origin_rejected(request: Request) -> JSONResponse | None:
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected:
        return error_response(
            403,
            "invalid_origin",
            "Запрос пришёл не с нашего сервера.",
        )
    return None


async def read_credentials(request: Request) -> tuple[str, str] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", "Нужны почта и пароль.")
    if not isinstance(body, dict):
        return error_response(400, "invalid_request", "Нужны почта и пароль.")
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return error_response(400, "invalid_request", "Нужны почта и пароль.")
    return email, password


LINK_EXPIRED = "Ссылка больше не действует"


class StoredPeriod(NamedTuple):
    started_at: datetime
    ends_at: datetime
    active: bool
    used: int


def find_stored_period(
    connection: Connection, account_id: uuid.UUID
) -> StoredPeriod | None:
    period = (
        connection.execute(
            select(subscription_periods)
            .where(subscription_periods.c.account_id == account_id)
            .order_by(
                subscription_periods.c.started_at.desc(),
                subscription_periods.c.id.desc(),
            )
            .limit(1)
        )
        .mappings()
        .first()
    )
    if period is None:
        return None
    used = connection.execute(
        select(quota_counters.c.used).where(
            quota_counters.c.period_id == period["id"]
        )
    ).scalar_one()
    return StoredPeriod(
        started_at=period["started_at"],
        ends_at=period["ends_at"],
        active=period["active"],
        used=used,
    )


def account_document(
    account_id: object,
    email: str,
    name: str,
    email_confirmed_at: datetime | None,
    stored_period: StoredPeriod | None = None,
    at: datetime | None = None,
) -> dict[str, object]:
    tariff_name = configured_tariff_name()
    tariff_price = configured_tariff_price()
    if email_confirmed_at is None:
        period = None
        quota = None
        quota_counter = None
        confirmed = False
    elif stored_period is None and payment_required():
        period = None
        quota = None
        quota_counter = None
        confirmed = True
    elif stored_period is None:
        period = {
            "started_at": format_utc(email_confirmed_at),
            "ends_at": format_utc(calendar_month_end(email_confirmed_at)),
            "active": True,
        }
        quota = None
        quota_counter = None
        confirmed = True
    else:
        active = stored_period.active
        if at is not None and at >= stored_period.ends_at:
            active = False
        period = {
            "started_at": format_utc(stored_period.started_at),
            "ends_at": format_utc(stored_period.ends_at),
            "active": active,
        }
        quota = development_tariff.model_requests - stored_period.used
        quota_counter = stored_period.used
        confirmed = True
    return {
        "account": {
            "id": str(account_id),
            "email": email,
            "name": name,
        },
        "email_confirmed": confirmed,
        "period": period,
        "quota": quota,
        "quota_counter": quota_counter,
        "tariff": {"name": tariff_name, "price": tariff_price},
    }


def opened_session(
    account_id: object,
    email: str,
    name: str,
    session_id: object,
    email_confirmed_at: datetime | None,
    stored_period: StoredPeriod | None = None,
    at: datetime | None = None,
) -> JSONResponse:
    response = JSONResponse(
        account_document(
            account_id,
            email,
            name,
            email_confirmed_at,
            stored_period,
            at,
        )
    )
    response.set_cookie(
        key="session",
        value=str(session_id),
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response


@contextmanager
def begin() -> Iterator[Connection]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


@router.post("/api/register")
async def register(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    credentials = await read_credentials(request)
    if isinstance(credentials, JSONResponse):
        return credentials
    email, password = credentials
    if not password_accepted(password):
        return error_response(400, "invalid_password", "Пароль не по правилам.")
    if len(normalize_email(email)) > EMAIL_MAX_LENGTH:
        return error_response(400, "invalid_email", "Почта длиннее 254 символов.")
    stored_email = normalize_email(email)
    try:
        with begin() as connection:
            account_id = create_account(connection, email, password)
            session_id = create_session(connection, account_id)
            token = issue_confirmation_link(connection, account_id)
    except PasswordNotAccepted:
        return error_response(400, "invalid_password", "Пароль не по правилам.")
    except EmailTooLong:
        return error_response(400, "invalid_email", "Почта длиннее 254 символов.")
    except IntegrityError:
        return error_response(400, "email_taken", "Эта почта уже зарегистрирована")
    log_confirmation_link(str(request.base_url), token, stored_email)
    return opened_session(
        account_id, stored_email, "", session_id, None, at=utc_now()
    )


@router.post("/api/login")
async def login(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    credentials = await read_credentials(request)
    if isinstance(credentials, JSONResponse):
        return credentials
    email, password = credentials
    now = utc_now()
    with begin() as connection:
        account = find_account_by_email(connection, email)
        if account is None or not verify_password(password, account["password_hash"]):
            opened = None
        else:
            confirmed_at = account["email_confirmed_at"]
            stored_period = (
                None
                if confirmed_at is None
                else find_stored_period(connection, account["id"])
            )
            opened = (
                account["id"],
                account["email"],
                account["name"],
                create_session(connection, account["id"]),
                confirmed_at,
                stored_period,
            )
    if opened is None:
        return error_response(
            400,
            "invalid_credentials",
            "Почта или пароль не сходятся",
        )
    account_id, stored_email, name, session_id, confirmed_at, stored_period = opened
    return opened_session(
        account_id,
        stored_email,
        name,
        session_id,
        confirmed_at,
        stored_period,
        now,
    )


def session_id_from_cookie(request: Request) -> uuid.UUID | None:
    raw = request.cookies.get("session")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def current_account(
    connection: Connection, request: Request, now: datetime
) -> tuple[RowMapping, RowMapping] | None:
    session_id = session_id_from_cookie(request)
    if session_id is None:
        return None
    session = find_session(connection, session_id)
    if session is None or not session_is_current(session["last_request_at"], now):
        return None
    account = find_account_by_id(connection, session["account_id"])
    if account is None:
        return None
    return session, account


def unauthenticated() -> JSONResponse:
    return error_response(401, "unauthenticated", "Нужна сессия.")


async def read_profile_name(request: Request) -> str | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    if not isinstance(body, dict) or set(body) != {"name"}:
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    name = body.get("name")
    if not isinstance(name, str) or len(name) > 80:
        return error_response(400, "invalid_name", "Имя профиля — до 80 символов.")
    return name


@router.patch("/api/me")
async def update_me(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    name = await read_profile_name(request)
    if isinstance(name, JSONResponse):
        return name
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            document = None
        else:
            session, account = current
            try:
                update_account_name(connection, account["id"], name)
            except ProfileNameRejected:
                document = "rejected"
            else:
                touch_session(connection, session["id"], now)
                confirmed_at = account["email_confirmed_at"]
                stored_period = (
                    None
                    if confirmed_at is None
                    else find_stored_period(connection, account["id"])
                )
                document = account_document(
                    account["id"],
                    account["email"],
                    name,
                    confirmed_at,
                    stored_period,
                )
    if document is None:
        return unauthenticated()
    if document == "rejected":
        return error_response(400, "invalid_name", "Имя профиля — до 80 символов.")
    return JSONResponse(document)


@router.get("/api/me")
def me(request: Request) -> JSONResponse:
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            document = None
        else:
            session, account = current
            touch_session(connection, session["id"], now)
            confirmed_at = account["email_confirmed_at"]
            stored_period = (
                None
                if confirmed_at is None
                else find_stored_period(connection, account["id"])
            )
            document = account_document(
                account["id"],
                account["email"],
                account["name"],
                confirmed_at,
                stored_period,
                now,
            )
    if document is None:
        return unauthenticated()
    return JSONResponse(document)


@router.get("/api/confirm-email")
def confirm_email(request: Request) -> Response:
    if "text/html" in request.headers.get("accept", "").lower():
        return HTMLResponse(_CABINET_HTML.read_text(encoding="utf-8"))
    now = utc_now()
    token = request.query_params.get("token")
    with begin() as connection:
        current = current_account(connection, request, now)
        if token is None:
            account_id = None
        else:
            account_id = apply_confirmation(connection, token, now)
    if account_id is not None:
        same_account = current is not None and current[1]["id"] == account_id
        return JSONResponse(
            {
                "email_confirmed": True,
                "destination": "bots" if same_account else "login",
            }
        )
    return JSONResponse(
        {
            "email_confirmed": False,
            "destination": "tariff" if current is not None else "login",
            "message": LINK_EXPIRED,
        }
    )


@router.post("/api/confirmation")
def request_confirmation(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            issued = None
        else:
            session, account = current
            if account["email_confirmed_at"] is not None:
                issued = "confirmed"
            else:
                token = replace_confirmation_link(connection, account["id"], now)
                touch_session(connection, session["id"], now)
                issued = (account["id"], account["email"], account["name"], token)
    if issued is None:
        return unauthenticated()
    if issued == "confirmed":
        return error_response(
            400,
            "email_already_confirmed",
            "Почта уже подтверждена.",
        )
    account_id, email, name, token = issued
    log_confirmation_link(str(request.base_url), token, email)
    return JSONResponse(account_document(account_id, email, name, None))


async def read_account_removal(request: Request) -> JSONResponse | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    if not isinstance(body, dict) or set(body) != {"confirm"}:
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    if body.get("confirm") is not True:
        return error_response(
            400,
            "confirmation_required",
            "Удаление нужно подтвердить.",
        )
    return None


@router.delete("/api/me")
async def delete_me(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    refusal = await read_account_removal(request)
    if refusal is not None:
        return refusal
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            removed = None
        else:
            removed = current[1]["id"]
            delete_account(connection, removed)
    if removed is None:
        return unauthenticated()
    from server.browser_action import forget_account

    forget_account(removed)
    response = JSONResponse({})
    response.delete_cookie(
        key="session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response


@router.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            removed = False
        else:
            from server.desktops import stop_routines_for_working_logout

            stop_routines_for_working_logout(
                connection, current[1]["id"], current[0]["id"], now
            )
            delete_session(connection, current[0]["id"])
            removed = True
    if not removed:
        return unauthenticated()
    response = JSONResponse({})
    response.delete_cookie(
        key="session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response
