import hashlib
import logging
import secrets
import sys
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.accounts import calendar_month_end, utc_now
from server.config import configured_mail_sink
from server.models import accounts, confirmation_links, quota_counters, subscription_periods

logger = logging.getLogger("server.confirmation")
CONFIRMATION_LIFETIME = timedelta(hours=24)


def _attach_journal_handler(target: logging.Logger) -> None:
    # uvicorn configures only its own loggers. The root logger stays at WARNING
    # with no handler, so this line has to write to the process stderr itself.
    target.setLevel(logging.INFO)
    target.propagate = False
    if target.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)


_attach_journal_handler(logger)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def confirmation_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/api/confirm-email?token={token}"


def confirmation_letter(base_url: str, token: str, email: str) -> str:
    return f"{confirmation_url(base_url, token)} {email}"


def issue_confirmation_link(connection: Connection, account_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe()
    connection.execute(
        confirmation_links.insert().values(
            id=uuid.uuid4(),
            account_id=account_id,
            token_hash=hash_token(token),
            expires_at=utc_now() + CONFIRMATION_LIFETIME,
            revoked_at=None,
        )
    )
    return token


def log_confirmation_link(base_url: str, token: str, email: str) -> None:
    letter = confirmation_letter(base_url, token, email)
    logger.info("%s", letter)
    path = configured_mail_sink()
    if path is None:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as sink:
        sink.write(f"{letter}\n")


def replace_confirmation_link(
    connection: Connection, account_id: uuid.UUID, now: datetime
) -> str:
    connection.execute(
        confirmation_links.update()
        .where(
            confirmation_links.c.account_id == account_id,
            confirmation_links.c.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return issue_confirmation_link(connection, account_id)


def apply_confirmation(
    connection: Connection, token: str, now: datetime
) -> uuid.UUID | None:
    if token == "":
        return None
    link = (
        connection.execute(
            select(confirmation_links).where(
                confirmation_links.c.token_hash == hash_token(token)
            )
        )
        .mappings()
        .one_or_none()
    )
    if link is None or link["revoked_at"] is not None or now > link["expires_at"]:
        return None
    updated = connection.execute(
        confirmation_links.update()
        .where(
            confirmation_links.c.id == link["id"],
            confirmation_links.c.revoked_at.is_(None),
            confirmation_links.c.expires_at >= now,
        )
        .values(revoked_at=now)
    )
    if updated.rowcount != 1:
        return None
    confirmed = connection.execute(
        accounts.update()
        .where(
            accounts.c.id == link["account_id"],
            accounts.c.email_confirmed_at.is_(None),
        )
        .values(email_confirmed_at=now)
    )
    if confirmed.rowcount == 1:
        period_id = uuid.uuid4()
        connection.execute(
            subscription_periods.insert().values(
                id=period_id,
                account_id=link["account_id"],
                started_at=now,
                ends_at=calendar_month_end(now),
                active=True,
            )
        )
        connection.execute(
            quota_counters.insert().values(
                id=uuid.uuid4(),
                period_id=period_id,
                used=0,
            )
        )
    return link["account_id"]
