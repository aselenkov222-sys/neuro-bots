from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)

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

subscription_periods = Table(
    "subscription_periods",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("active", Boolean(), nullable=False),
)

quota_counters = Table(
    "quota_counters",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "period_id",
        Uuid(),
        ForeignKey("subscription_periods.id"),
        nullable=False,
        unique=True,
    ),
    Column("used", Integer(), nullable=False),
)

quota_charges = Table(
    "quota_charges",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column(
        "period_id",
        Uuid(),
        ForeignKey("subscription_periods.id"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

bots = Table(
    "bots",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("name", String(80), nullable=False),
    Column("color", String(7), nullable=False),
    Column("icon", String(32), nullable=False),
    Column("description", String(280), nullable=False),
    Column("scenario", String(16000), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

chats = Table(
    "chats",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("bot_id", Uuid(), ForeignKey("bots.id"), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("chat_id", Uuid(), ForeignKey("chats.id"), nullable=False),
    Column("role", String(16), nullable=False),
    Column("text", Text(), nullable=False),
    Column("code", Text(), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "role IN ('user', 'assistant', 'error')",
        name="messages_role_check",
    ),
)

desktops = Table(
    "desktops",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("account_id", Uuid(), ForeignKey("accounts.id"), nullable=False),
    Column("computer_id", Uuid(), nullable=False),
    Column("working", Boolean(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("timezone", Text(), nullable=True),
    Column("last_pulse_at", DateTime(timezone=True), nullable=True),
    Column("session_id", Uuid(), nullable=True),
    Column(
        "routines_stopped",
        Boolean(),
        nullable=False,
        server_default=text("false"),
    ),
    UniqueConstraint("account_id", "computer_id"),
    Index(
        "desktops_one_working",
        "account_id",
        unique=True,
        postgresql_where=text("working"),
    ),
)

routines = Table(
    "routines",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("bot_id", Uuid(), ForeignKey("bots.id"), nullable=False),
    Column("weekdays", ARRAY(Integer()), nullable=False),
    Column("time_of_day", Time(timezone=False), nullable=False),
    Column("paused", Boolean(), nullable=False, server_default=text("false")),
)

routine_runs = Table(
    "routine_runs",
    metadata,
    Column("id", Uuid(), primary_key=True),
    Column("routine_id", Uuid(), ForeignKey("routines.id"), nullable=False),
    Column("local_date", Date(), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('pending', 'claimed', 'skipped')",
        name="routine_runs_status_check",
    ),
    UniqueConstraint("routine_id", "local_date"),
)
