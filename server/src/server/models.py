from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, Uuid, text

from server.db import metadata

accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("password_hash", Text(), nullable=False),
    Column("name", String(80), nullable=False, server_default=text("''")),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("last_request_at", DateTime(timezone=True), nullable=False),
)
