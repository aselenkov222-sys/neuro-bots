import calendar
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.models import accounts, sessions
from server.passwords import PasswordNotAccepted, hash_password, password_accepted

EMAIL_MAX_LENGTH = 254
PROFILE_NAME_MAX_LENGTH = 80
SESSION_LIFETIME = timedelta(days=30)


class ProfileNameRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("profile name is longer than 80 characters")


class EmailTooLong(ValueError):
    def __init__(self) -> None:
        super().__init__("email is longer than 254 characters")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_account(connection: Connection, email: str, password: str) -> uuid.UUID:
    if not password_accepted(password):
        raise PasswordNotAccepted
    stored_email = normalize_email(email)
    if len(stored_email) > EMAIL_MAX_LENGTH:
        raise EmailTooLong
    account_id = uuid.uuid4()
    connection.execute(
        accounts.insert().values(
            id=account_id,
            email=stored_email,
            password_hash=hash_password(password),
        )
    )
    return account_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        normalized = moment.replace(tzinfo=timezone.utc)
    else:
        normalized = moment.astimezone(timezone.utc)
    return normalized.isoformat()


def calendar_month_end(start: datetime) -> datetime:
    if start.month == 12:
        year = start.year + 1
        month = 1
    else:
        year = start.year
        month = start.month + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def session_is_current(last_request_at: datetime, now: datetime) -> bool:
    return now < last_request_at + SESSION_LIFETIME


def create_session(connection: Connection, account_id: uuid.UUID) -> uuid.UUID:
    session_id = uuid.uuid4()
    connection.execute(
        sessions.insert().values(
            id=session_id,
            account_id=account_id,
            last_request_at=utc_now(),
        )
    )
    return session_id


def find_session(connection: Connection, session_id: uuid.UUID) -> RowMapping | None:
    return (
        connection.execute(select(sessions).where(sessions.c.id == session_id))
        .mappings()
        .one_or_none()
    )


def find_account_by_id(connection: Connection, account_id: uuid.UUID) -> RowMapping | None:
    return (
        connection.execute(select(accounts).where(accounts.c.id == account_id))
        .mappings()
        .one_or_none()
    )


def touch_session(connection: Connection, session_id: uuid.UUID, now: datetime) -> None:
    connection.execute(
        sessions.update()
        .where(sessions.c.id == session_id)
        .values(last_request_at=now)
    )


def delete_session(connection: Connection, session_id: uuid.UUID) -> None:
    connection.execute(sessions.delete().where(sessions.c.id == session_id))


def update_account_name(connection: Connection, account_id: uuid.UUID, name: str) -> None:
    if len(name) > PROFILE_NAME_MAX_LENGTH:
        raise ProfileNameRejected
    connection.execute(
        accounts.update().where(accounts.c.id == account_id).values(name=name)
    )


def find_account_by_email(connection: Connection, email: str) -> RowMapping | None:
    stored_email = normalize_email(email)
    return (
        connection.execute(
            select(accounts).where(accounts.c.email == stored_email)
        )
        .mappings()
        .one_or_none()
    )
