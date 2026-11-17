from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, Uuid, text

from server.db import metadata

accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("password_hash", Text(), nullable=False),
    Column("name", String(80), nullable=False, server_default=text("''")),
    Column("email_confirmed_at", DateTime(timezone=True), nullable=True),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("last_request_at", DateTime(timezone=True), nullable=False),
)

confirmation_links = Table(
    "confirmation_links",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("token_hash", Text(), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)
