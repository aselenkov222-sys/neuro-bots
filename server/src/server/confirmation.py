import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from sqlalchemy.engine import Connection

from server.accounts import utc_now
from server.models import confirmation_links

logger = logging.getLogger("server.confirmation")

CONFIRMATION_LIFETIME = timedelta(hours=24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def confirmation_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/api/confirm-email?token={token}"


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
    logger.info("%s %s", confirmation_url(base_url, token), email)
