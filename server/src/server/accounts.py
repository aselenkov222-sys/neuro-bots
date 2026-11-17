import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.models import accounts, sessions
from server.passwords import PasswordNotAccepted, hash_password, password_accepted

EMAIL_MAX_LENGTH = 254


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


def create_session(connection: Connection, account_id: uuid.UUID) -> uuid.UUID:
    session_id = uuid.uuid4()
    connection.execute(
        sessions.insert().values(
            id=session_id,
            account_id=account_id,
            last_request_at=datetime.now(timezone.utc),
        )
    )
    return session_id


def find_account_by_email(connection: Connection, email: str) -> RowMapping | None:
    stored_email = normalize_email(email)
    return (
        connection.execute(
            select(accounts).where(accounts.c.email == stored_email)
        )
        .mappings()
        .one_or_none()
    )
