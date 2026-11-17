import json
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from server.accounts import (
    EMAIL_MAX_LENGTH,
    EmailTooLong,
    create_account,
    create_session,
    find_account_by_email,
    normalize_email,
)
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


def opened_session(
    account_id: object,
    email: str,
    name: str,
    session_id: object,
) -> JSONResponse:
    response = JSONResponse(
        {
            "account": {
                "id": str(account_id),
                "email": email,
                "name": name,
            },
            "email_confirmed": False,
            "period": None,
            "quota": None,
        }
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
    try:
        with begin() as connection:
            account_id = create_account(connection, email, password)
            session_id = create_session(connection, account_id)
    except PasswordNotAccepted:
        return error_response(400, "invalid_password", "Пароль не по правилам.")
    except EmailTooLong:
        return error_response(400, "invalid_email", "Почта длиннее 254 символов.")
    except IntegrityError:
        return error_response(400, "email_taken", "Эта почта уже зарегистрирована")
    return opened_session(account_id, normalize_email(email), "", session_id)


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
