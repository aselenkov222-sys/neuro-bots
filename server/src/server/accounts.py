import calendar
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.models import (
    accounts,
    bots,
    catalog_apps,
    confirmation_links,
    desktop_notices,
    desktops,
    quota_charges,
    quota_counters,
    sessions,
    subscription_periods,
)
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


def delete_account(connection: Connection, account_id: uuid.UUID) -> None:
    from server.bots import _remove_bot

    bot_ids = connection.execute(
        select(bots.c.id).where(bots.c.account_id == account_id)
    ).scalars()
    for bot_id in bot_ids:
        _remove_bot(connection, bot_id)
    period_ids = select(subscription_periods.c.id).where(
        subscription_periods.c.account_id == account_id
    )
    connection.execute(
        quota_charges.delete().where(quota_charges.c.period_id.in_(period_ids))
    )
    connection.execute(
        quota_counters.delete().where(quota_counters.c.period_id.in_(period_ids))
    )
    connection.execute(
        subscription_periods.delete().where(
            subscription_periods.c.account_id == account_id
        )
    )
    connection.execute(
        catalog_apps.delete().where(catalog_apps.c.account_id == account_id)
    )
    connection.execute(
        desktop_notices.delete().where(desktop_notices.c.account_id == account_id)
    )
    connection.execute(
        confirmation_links.delete().where(
            confirmation_links.c.account_id == account_id
        )
    )
    connection.execute(desktops.delete().where(desktops.c.account_id == account_id))
    connection.execute(sessions.delete().where(sessions.c.account_id == account_id))
    connection.execute(accounts.delete().where(accounts.c.id == account_id))


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
