import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from server.accounts import (
    EMAIL_MAX_LENGTH,
    EmailTooLong,
    create_account,
    create_session,
    delete_session,
    find_account_by_email,
    find_account_by_id,
    find_session,
    normalize_email,
    session_is_current,
    touch_session,
    utc_now,
)
from server.confirmation import issue_confirmation_link, log_confirmation_link
from server.db import create_db_engine
from server.passwords import PasswordNotAccepted, password_accepted, verify_password

router = APIRouter()


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


def account_document(account_id: object, email: str, name: str) -> dict[str, object]:
    return {
        "account": {
            "id": str(account_id),
            "email": email,
            "name": name,
        },
        "email_confirmed": False,
        "period": None,
        "quota": None,
    }


def opened_session(
    account_id: object,
    email: str,
    name: str,
    session_id: object,
) -> JSONResponse:
    response = JSONResponse(account_document(account_id, email, name))
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
    return opened_session(account_id, stored_email, "", session_id)


@router.post("/api/login")
async def login(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    credentials = await read_credentials(request)
    if isinstance(credentials, JSONResponse):
        return credentials
    email, password = credentials
    with begin() as connection:
        account = find_account_by_email(connection, email)
        if account is None or not verify_password(password, account["password_hash"]):
            opened = None
        else:
            opened = (
                account["id"],
                account["email"],
                account["name"],
                create_session(connection, account["id"]),
            )
    if opened is None:
        return error_response(
            400,
            "invalid_credentials",
            "Почта или пароль не сходятся",
        )
    account_id, stored_email, name, session_id = opened
    return opened_session(account_id, stored_email, name, session_id)


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
            document = account_document(account["id"], account["email"], account["name"])
    if document is None:
        return unauthenticated()
    return JSONResponse(document)


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
