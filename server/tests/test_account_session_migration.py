import ast
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from server.db import create_db_engine, metadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"

ACCOUNT_COLUMNS = ("id", "email", "password_hash", "name", "email_confirmed_at")
SESSION_COLUMNS = ("id", "account_id", "last_request_at")
CONFIRMATION_LINK_COLUMNS = (
    "id",
    "account_id",
    "token_hash",
    "expires_at",
    "revoked_at",
)
SUBSCRIPTION_PERIOD_COLUMNS = (
    "id",
    "account_id",
    "started_at",
    "ends_at",
    "active",
)
QUOTA_COUNTER_COLUMNS = ("id", "period_id", "used")
QUOTA_CHARGE_COLUMNS = ("id", "period_id", "created_at")
BOT_COLUMNS = (
    "id",
    "account_id",
    "name",
    "color",
    "icon",
    "description",
    "scenario",
    "updated_at",
)
CHAT_COLUMNS = ("id", "bot_id", "updated_at")
MESSAGE_COLUMNS = ("id", "chat_id", "role", "text", "code", "updated_at")
DESKTOP_COLUMNS = (
    "id",
    "account_id",
    "computer_id",
    "working",
    "created_at",
    "timezone",
    "last_pulse_at",
    "session_id",
    "routines_stopped",
)
ROUTINE_COLUMNS = ("id", "bot_id", "weekdays", "time_of_day", "paused")
ACCOUNT_REVISION = "3c87a0f84d5e"
CONFIRMATION_REVISION = "ff582d8bc7fb"
EMAIL_CONFIRMED_REVISION = "048ee727aa3e"
SUBSCRIPTION_REVISION = "c54ff0c6fb42"
DESKTOP_REVISION = "78372be7d547"
PULSE_REVISION = "f524099440d0"
ROUTINE_REVISION = "b7e2c4a91d58"
PAUSED_REVISION = "a4f0c8e21b67"
STOPPED_REVISION = "9e4b1c7a6d20"
RUN_REVISION = "f7a2c6e81d04"
CASCADE_REVISION = "a1c4e8b27d60"
CANCELLED_REVISION = "d4f1b8e06c32"
ROUTINE_RUN_COLUMNS = (
    "id",
    "routine_id",
    "local_date",
    "due_at",
    "status",
    "claimed_at",
)
ROUTINE_RUN_STATUS_CHECK = (
    "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'claimed'::character varying, 'skipped'::character varying, "
    "'cancelled'::character varying, 'failed'::character varying])::text[])))"
)
MESSAGE_ROLE_CHECK = (
    "CHECK (((role)::text = ANY ((ARRAY['user'::character varying, "
    "'assistant'::character varying, 'error'::character varying])::text[])))"
)


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(PROJECT_ROOT),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _revision_links() -> dict[str, str | None]:
    scripts = list(VERSIONS_DIR.glob("*.py"))
    assert scripts
    links: dict[str, str | None] = {}
    for script in scripts:
        module = ast.parse(script.read_text())
        revision: str | None = None
        down_revision: str | None = None
        saw_down = False
        for node in module.body:
            if not (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Constant)
            ):
                continue
            value = node.value.value
            if node.target.id == "revision" and isinstance(value, str):
                revision = value
            elif node.target.id == "down_revision" and (
                value is None or isinstance(value, str)
            ):
                saw_down = True
                down_revision = value
        if revision is None or not saw_down:
            raise AssertionError("migration revision id is missing")
        assert revision not in links
        links[revision] = down_revision
    return links


def _revision_chain() -> list[str]:
    links = _revision_links()
    roots = [revision for revision, down in links.items() if down is None]
    assert roots == [ACCOUNT_REVISION]
    chain = [roots[0]]
    while True:
        children = [
            revision for revision, down in links.items() if down == chain[-1]
        ]
        if not children:
            break
        assert len(children) == 1
        chain.append(children[0])
    assert set(chain) == set(links)
    assert links[CONFIRMATION_REVISION] == ACCOUNT_REVISION
    assert links[EMAIL_CONFIRMED_REVISION] == CONFIRMATION_REVISION
    assert links[SUBSCRIPTION_REVISION] == EMAIL_CONFIRMED_REVISION
    assert links[DESKTOP_REVISION] == SUBSCRIPTION_REVISION
    assert links[PULSE_REVISION] == DESKTOP_REVISION
    assert links[ROUTINE_REVISION] == PULSE_REVISION
    assert links[PAUSED_REVISION] == ROUTINE_REVISION
    assert links[STOPPED_REVISION] == PAUSED_REVISION
    assert links[RUN_REVISION] == STOPPED_REVISION
    assert links[CASCADE_REVISION] == RUN_REVISION
    assert links[CANCELLED_REVISION] == CASCADE_REVISION
    assert links[chain[-1]] == CANCELLED_REVISION
    assert chain[-1] != CONFIRMATION_REVISION
    assert chain[-1] != EMAIL_CONFIRMED_REVISION
    assert chain[-1] != SUBSCRIPTION_REVISION
    assert chain[-1] != DESKTOP_REVISION
    assert chain[-1] != PULSE_REVISION
    assert chain[-1] != ROUTINE_REVISION
    assert chain[-1] != PAUSED_REVISION
    assert chain[-1] != STOPPED_REVISION
    assert chain[-1] != RUN_REVISION
    assert chain[-1] != CASCADE_REVISION
    assert chain[-1] != CANCELLED_REVISION
    return chain


def _revision_id() -> str:
    return _revision_chain()[-1]


def _columns(connection: Connection, table_name: str) -> dict[str, dict[str, object]]:
    rows = (
        connection.execute(
            text(
                """
                SELECT
                    a.attname AS column_name,
                    a.attnotnull AS not_null,
                    pg_catalog.format_type(a.atttypid, a.atttypmod) AS format_type,
                    t.typname AS type_name,
                    pg_get_expr(ad.adbin, ad.adrelid) AS column_default
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
                LEFT JOIN pg_catalog.pg_attrdef AS ad
                    ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                WHERE n.nspname = 'public'
                  AND c.relname = :table_name
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum
                """
            ),
            {"table_name": table_name},
        )
        .mappings()
        .all()
    )
    return {str(row["column_name"]): dict(row) for row in rows}


def _indexes(connection: Connection, table_name: str) -> list[dict[str, object]]:
    rows = (
        connection.execute(
            text(
                """
                SELECT
                    i.relname AS index_name,
                    ix.indisunique AS is_unique,
                    pg_get_expr(ix.indpred, ix.indrelid) AS predicate,
                    (
                        SELECT array_agg(a.attname ORDER BY k.ordinality)
                        FROM unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ordinality)
                        JOIN pg_catalog.pg_attribute AS a
                          ON a.attrelid = t.oid AND a.attnum = k.attnum
                    ) AS columns
                FROM pg_catalog.pg_index AS ix
                JOIN pg_catalog.pg_class AS i ON i.oid = ix.indexrelid
                JOIN pg_catalog.pg_class AS t ON t.oid = ix.indrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public'
                  AND t.relname = :table_name
                """
            ),
            {"table_name": table_name},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _constraints(connection: Connection, table_name: str) -> set[tuple[str, str]]:
    rows = (
        connection.execute(
            text(
                """
                SELECT c.contype AS constraint_type,
                       pg_get_constraintdef(c.oid) AS definition
                FROM pg_catalog.pg_constraint AS c
                JOIN pg_catalog.pg_class AS rel ON rel.oid = c.conrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = rel.relnamespace
                WHERE n.nspname = 'public'
                  AND rel.relname = :table_name
                """
            ),
            {"table_name": table_name},
        )
        .mappings()
        .all()
    )
    return {
        (str(row["constraint_type"]), str(row["definition"])) for row in rows
    }


def _assert_model_column(
    column: object,
    type_: type[object],
    *,
    nullable: bool,
    length: int | None = None,
    timezone: bool | None = None,
    unique: bool = False,
    foreign_table: str | None = None,
    ondelete: str | None = None,
) -> None:
    assert isinstance(column.type, type_)
    assert column.nullable is nullable
    assert column.default is None
    assert column.server_default is None
    if length is not None:
        assert column.type.length == length
    if timezone is not None:
        assert column.type.timezone is timezone
    assert column.unique is (True if unique else None)
    foreign_keys = list(column.foreign_keys)
    if foreign_table is None:
        assert foreign_keys == []
    else:
        assert len(foreign_keys) == 1
        assert foreign_keys[0].column.table.name == foreign_table
        assert foreign_keys[0].column.name == "id"
        assert foreign_keys[0].ondelete == ondelete


def _assert_catalog_column(
    columns: dict[str, dict[str, object]],
    name: str,
    type_name: str,
    format_type: str,
    *,
    not_null: bool,
) -> None:
    assert columns[name]["type_name"] == type_name
    assert columns[name]["format_type"] == format_type
    assert columns[name]["not_null"] is not_null
    assert columns[name]["column_default"] is None


def _insert_accepted_message_roles(connection: Connection) -> None:
    account_id = uuid.uuid4()
    bot_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    moment = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    connection.execute(
        text(
            """
            INSERT INTO accounts (id, email, password_hash, name)
            VALUES (:id, :email, :password_hash, :name)
            """
        ),
        {
            "id": account_id,
            "email": "message-role@example.com",
            "password_hash": "not-a-password",
            "name": "",
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO bots (
                id, account_id, name, color, icon,
                description, scenario, updated_at
            )
            VALUES (
                :id, :account_id, :name, :color, :icon,
                :description, :scenario, :updated_at
            )
            """
        ),
        {
            "id": bot_id,
            "account_id": account_id,
            "name": "Bot",
            "color": "#c85a12",
            "icon": "spark",
            "description": "",
            "scenario": "",
            "updated_at": moment,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO chats (id, bot_id, updated_at)
            VALUES (:id, :bot_id, :updated_at)
            """
        ),
        {"id": chat_id, "bot_id": bot_id, "updated_at": moment},
    )
    for role in ("user", "assistant", "error"):
        connection.execute(
            text(
                """
                INSERT INTO messages (id, chat_id, role, text, code, updated_at)
                VALUES (:id, :chat_id, :role, :text, :code, :updated_at)
                """
            ),
            {
                "id": uuid.uuid4(),
                "chat_id": chat_id,
                "role": role,
                "text": role,
                "code": None,
                "updated_at": moment,
            },
        )
    stored = (
        connection.execute(text("SELECT role FROM messages ORDER BY role"))
        .scalars()
        .all()
    )
    assert tuple(stored) == ("assistant", "error", "user")
    try:
        connection.execute(
            text(
                """
                INSERT INTO messages (id, chat_id, role, text, code, updated_at)
                VALUES (:id, :chat_id, :role, :text, :code, :updated_at)
                """
            ),
            {
                "id": uuid.uuid4(),
                "chat_id": chat_id,
                "role": "system",
                "text": "no",
                "code": None,
                "updated_at": moment,
            },
        )
    except IntegrityError:
        return
    raise AssertionError("message role system was stored")


def test_upgrade_head_creates_accounts_and_sessions() -> None:
    import server.models

    accounts = metadata.tables["accounts"]
    sessions = metadata.tables["sessions"]
    confirmation_links = metadata.tables["confirmation_links"]
    subscription_periods = metadata.tables["subscription_periods"]
    quota_counters = metadata.tables["quota_counters"]
    quota_charges = metadata.tables["quota_charges"]
    bots = metadata.tables["bots"]
    chats = metadata.tables["chats"]
    messages = metadata.tables["messages"]
    desktops = metadata.tables["desktops"]
    routines = metadata.tables["routines"]
    routine_runs = metadata.tables["routine_runs"]
    assert set(metadata.tables) == {
        "accounts",
        "sessions",
        "confirmation_links",
        "subscription_periods",
        "quota_counters",
        "quota_charges",
        "bots",
        "chats",
        "messages",
        "desktops",
        "routines",
        "routine_runs",
    }
    assert accounts is server.models.accounts
    assert sessions is server.models.sessions
    assert confirmation_links is server.models.confirmation_links
    assert subscription_periods is server.models.subscription_periods
    assert quota_counters is server.models.quota_counters
    assert quota_charges is server.models.quota_charges
    assert bots is server.models.bots
    assert chats is server.models.chats
    assert messages is server.models.messages
    assert desktops is server.models.desktops
    assert routines is server.models.routines
    assert routine_runs is server.models.routine_runs
    assert tuple(accounts.columns.keys()) == ACCOUNT_COLUMNS
    assert tuple(sessions.columns.keys()) == SESSION_COLUMNS
    assert tuple(confirmation_links.columns.keys()) == CONFIRMATION_LINK_COLUMNS
    assert tuple(subscription_periods.columns.keys()) == SUBSCRIPTION_PERIOD_COLUMNS
    assert tuple(quota_counters.columns.keys()) == QUOTA_COUNTER_COLUMNS
    assert tuple(quota_charges.columns.keys()) == QUOTA_CHARGE_COLUMNS
    assert tuple(bots.columns.keys()) == BOT_COLUMNS
    assert tuple(chats.columns.keys()) == CHAT_COLUMNS
    assert tuple(messages.columns.keys()) == MESSAGE_COLUMNS
    assert tuple(desktops.columns.keys()) == DESKTOP_COLUMNS
    assert tuple(routines.columns.keys()) == ROUTINE_COLUMNS
    assert tuple(routine_runs.columns.keys()) == ROUTINE_RUN_COLUMNS

    assert isinstance(accounts.c.id.type, Uuid)
    assert accounts.c.id.nullable is False
    assert [column.name for column in accounts.primary_key] == ["id"]
    assert isinstance(accounts.c.email.type, String)
    assert accounts.c.email.type.length == 254
    assert accounts.c.email.nullable is False
    assert accounts.c.email.unique is True
    assert isinstance(accounts.c.password_hash.type, Text)
    assert accounts.c.password_hash.nullable is False
    assert isinstance(accounts.c.name.type, String)
    assert accounts.c.name.type.length == 80
    assert accounts.c.name.nullable is False
    assert accounts.c.name.server_default.arg.text == "''"
    assert isinstance(accounts.c.email_confirmed_at.type, DateTime)
    assert accounts.c.email_confirmed_at.type.timezone is True
    assert accounts.c.email_confirmed_at.nullable is True
    assert accounts.c.email_confirmed_at.default is None
    assert accounts.c.email_confirmed_at.server_default is None

    assert isinstance(sessions.c.id.type, Uuid)
    assert sessions.c.id.nullable is False
    assert [column.name for column in sessions.primary_key] == ["id"]
    assert isinstance(sessions.c.account_id.type, Uuid)
    assert sessions.c.account_id.nullable is False
    foreign_keys = list(sessions.c.account_id.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].column.table.name == "accounts"
    assert foreign_keys[0].column.name == "id"
    assert foreign_keys[0].ondelete is None
    assert isinstance(sessions.c.last_request_at.type, DateTime)
    assert sessions.c.last_request_at.type.timezone is True
    assert sessions.c.last_request_at.nullable is False

    assert isinstance(confirmation_links.c.id.type, Uuid)
    assert confirmation_links.c.id.nullable is False
    assert confirmation_links.c.id.default is None
    assert confirmation_links.c.id.server_default is None
    assert [column.name for column in confirmation_links.primary_key] == ["id"]
    assert isinstance(confirmation_links.c.account_id.type, Uuid)
    assert confirmation_links.c.account_id.nullable is False
    assert confirmation_links.c.account_id.default is None
    assert confirmation_links.c.account_id.server_default is None
    link_foreign_keys = list(confirmation_links.c.account_id.foreign_keys)
    assert len(link_foreign_keys) == 1
    assert link_foreign_keys[0].column.table.name == "accounts"
    assert link_foreign_keys[0].column.name == "id"
    assert link_foreign_keys[0].ondelete is None
    assert isinstance(confirmation_links.c.token_hash.type, Text)
    assert confirmation_links.c.token_hash.nullable is False
    assert confirmation_links.c.token_hash.unique is True
    assert confirmation_links.c.token_hash.default is None
    assert confirmation_links.c.token_hash.server_default is None
    assert isinstance(confirmation_links.c.expires_at.type, DateTime)
    assert confirmation_links.c.expires_at.type.timezone is True
    assert confirmation_links.c.expires_at.nullable is False
    assert confirmation_links.c.expires_at.default is None
    assert confirmation_links.c.expires_at.server_default is None
    assert isinstance(confirmation_links.c.revoked_at.type, DateTime)
    assert confirmation_links.c.revoked_at.type.timezone is True
    assert confirmation_links.c.revoked_at.nullable is True
    assert confirmation_links.c.revoked_at.default is None
    assert confirmation_links.c.revoked_at.server_default is None

    assert [column.name for column in subscription_periods.primary_key] == ["id"]
    _assert_model_column(subscription_periods.c.id, Uuid, nullable=False)
    _assert_model_column(
        subscription_periods.c.account_id,
        Uuid,
        nullable=False,
        foreign_table="accounts",
    )
    _assert_model_column(
        subscription_periods.c.started_at,
        DateTime,
        nullable=False,
        timezone=True,
    )
    _assert_model_column(
        subscription_periods.c.ends_at,
        DateTime,
        nullable=False,
        timezone=True,
    )
    _assert_model_column(subscription_periods.c.active, Boolean, nullable=False)

    assert [column.name for column in quota_counters.primary_key] == ["id"]
    _assert_model_column(quota_counters.c.id, Uuid, nullable=False)
    _assert_model_column(
        quota_counters.c.period_id,
        Uuid,
        nullable=False,
        unique=True,
        foreign_table="subscription_periods",
    )
    _assert_model_column(quota_counters.c.used, Integer, nullable=False)

    assert [column.name for column in quota_charges.primary_key] == ["id"]
    _assert_model_column(quota_charges.c.id, Uuid, nullable=False)
    _assert_model_column(
        quota_charges.c.period_id,
        Uuid,
        nullable=False,
        foreign_table="subscription_periods",
    )
    _assert_model_column(
        quota_charges.c.created_at,
        DateTime,
        nullable=False,
        timezone=True,
    )

    assert [column.name for column in bots.primary_key] == ["id"]
    _assert_model_column(bots.c.id, Uuid, nullable=False)
    _assert_model_column(
        bots.c.account_id,
        Uuid,
        nullable=False,
        foreign_table="accounts",
    )
    _assert_model_column(bots.c.name, String, nullable=False, length=80)
    _assert_model_column(bots.c.color, String, nullable=False, length=7)
    _assert_model_column(bots.c.icon, String, nullable=False, length=32)
    _assert_model_column(bots.c.description, String, nullable=False, length=280)
    _assert_model_column(bots.c.scenario, String, nullable=False, length=16000)
    _assert_model_column(
        bots.c.updated_at,
        DateTime,
        nullable=False,
        timezone=True,
    )

    assert [column.name for column in chats.primary_key] == ["id"]
    _assert_model_column(chats.c.id, Uuid, nullable=False)
    _assert_model_column(
        chats.c.bot_id,
        Uuid,
        nullable=False,
        foreign_table="bots",
    )
    _assert_model_column(
        chats.c.updated_at,
        DateTime,
        nullable=False,
        timezone=True,
    )

    assert [column.name for column in messages.primary_key] == ["id"]
    _assert_model_column(messages.c.id, Uuid, nullable=False)
    _assert_model_column(
        messages.c.chat_id,
        Uuid,
        nullable=False,
        foreign_table="chats",
    )
    _assert_model_column(messages.c.role, String, nullable=False, length=16)
    _assert_model_column(messages.c.text, Text, nullable=False)
    _assert_model_column(messages.c.code, Text, nullable=True)
    _assert_model_column(
        messages.c.updated_at,
        DateTime,
        nullable=False,
        timezone=True,
    )
    assert [
        (constraint.name, str(constraint.sqltext))
        for constraint in messages.constraints
        if isinstance(constraint, CheckConstraint)
    ] == [("messages_role_check", "role IN ('user', 'assistant', 'error')")]
    for table in (
        subscription_periods,
        quota_counters,
        quota_charges,
        bots,
        chats,
        desktops,
        routines,
    ):
        assert [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        ] == []

    assert [column.name for column in desktops.primary_key] == ["id"]
    _assert_model_column(desktops.c.id, Uuid, nullable=False)
    _assert_model_column(
        desktops.c.account_id,
        Uuid,
        nullable=False,
        foreign_table="accounts",
    )
    _assert_model_column(desktops.c.computer_id, Uuid, nullable=False)
    _assert_model_column(desktops.c.working, Boolean, nullable=False)
    _assert_model_column(
        desktops.c.created_at,
        DateTime,
        nullable=False,
        timezone=True,
    )
    _assert_model_column(desktops.c.timezone, Text, nullable=True)
    _assert_model_column(
        desktops.c.last_pulse_at,
        DateTime,
        nullable=True,
        timezone=True,
    )
    _assert_model_column(desktops.c.session_id, Uuid, nullable=True)
    assert isinstance(desktops.c.routines_stopped.type, Boolean)
    assert desktops.c.routines_stopped.nullable is False
    assert desktops.c.routines_stopped.default is None
    assert desktops.c.routines_stopped.server_default.arg.text == "false"
    assert list(desktops.c.routines_stopped.foreign_keys) == []
    unique_constraints = [
        constraint
        for constraint in desktops.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert len(unique_constraints) == 1
    assert [column.name for column in unique_constraints[0].columns] == [
        "account_id",
        "computer_id",
    ]
    assert [index.name for index in desktops.indexes] == ["desktops_one_working"]
    partial_index = next(iter(desktops.indexes))
    assert isinstance(partial_index, Index)
    assert partial_index.unique is True
    assert [column.name for column in partial_index.columns] == ["account_id"]
    assert str(partial_index.dialect_options["postgresql"]["where"]) == "working"

    assert [column.name for column in routines.primary_key] == ["id"]
    _assert_model_column(routines.c.id, Uuid, nullable=False)
    _assert_model_column(
        routines.c.bot_id,
        Uuid,
        nullable=False,
        foreign_table="bots",
    )
    _assert_model_column(routines.c.weekdays, ARRAY, nullable=False)
    assert isinstance(routines.c.weekdays.type.item_type, Integer)
    _assert_model_column(
        routines.c.time_of_day,
        Time,
        nullable=False,
        timezone=False,
    )
    assert isinstance(routines.c.paused.type, Boolean)
    assert routines.c.paused.nullable is False
    assert routines.c.paused.default is None
    assert routines.c.paused.server_default.arg.text == "false"
    assert [
        constraint
        for constraint in routines.constraints
        if isinstance(constraint, CheckConstraint)
    ] == []

    assert [column.name for column in routine_runs.primary_key] == ["id"]
    _assert_model_column(routine_runs.c.id, Uuid, nullable=False)
    _assert_model_column(
        routine_runs.c.routine_id,
        Uuid,
        nullable=False,
        foreign_table="routines",
        ondelete="CASCADE",
    )
    _assert_model_column(routine_runs.c.local_date, Date, nullable=False)
    _assert_model_column(
        routine_runs.c.due_at,
        DateTime,
        nullable=False,
        timezone=True,
    )
    _assert_model_column(routine_runs.c.status, String, nullable=False, length=16)
    _assert_model_column(
        routine_runs.c.claimed_at,
        DateTime,
        nullable=True,
        timezone=True,
    )
    assert [
        (constraint.name, str(constraint.sqltext))
        for constraint in routine_runs.constraints
        if isinstance(constraint, CheckConstraint)
    ] == [
        (
            "routine_runs_status_check",
            "status IN ('pending', 'claimed', 'skipped', 'cancelled', 'failed')",
        )
    ]
    run_uniques = [
        constraint
        for constraint in routine_runs.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert len(run_uniques) == 1
    assert [column.name for column in run_uniques[0].columns] == [
        "routine_id",
        "local_date",
    ]
    for column in routine_runs.columns:
        assert not isinstance(column.type, Text)

    revision_id = _revision_id()
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout

        engine = create_db_engine()
        try:
            with engine.connect() as connection:
                tables = (
                    connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables"
                            " WHERE schemaname = 'public'"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert set(tables) == {
                    "accounts",
                    "sessions",
                    "confirmation_links",
                    "subscription_periods",
                    "quota_counters",
                    "quota_charges",
                    "bots",
                    "chats",
                    "messages",
                    "desktops",
                    "routines",
                    "routine_runs",
                    "alembic_version",
                }
                stored_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                assert stored_revision == revision_id

                account_columns = _columns(connection, "accounts")
                assert tuple(account_columns) == ACCOUNT_COLUMNS
                assert account_columns["id"]["type_name"] == "uuid"
                assert account_columns["id"]["format_type"] == "uuid"
                assert account_columns["id"]["not_null"] is True
                assert account_columns["id"]["column_default"] is None
                assert account_columns["email"]["type_name"] == "varchar"
                assert account_columns["email"]["format_type"] == "character varying(254)"
                assert account_columns["email"]["not_null"] is True
                assert account_columns["email"]["column_default"] is None
                assert account_columns["password_hash"]["type_name"] == "text"
                assert account_columns["password_hash"]["format_type"] == "text"
                assert account_columns["password_hash"]["not_null"] is True
                assert account_columns["password_hash"]["column_default"] is None
                assert account_columns["name"]["type_name"] == "varchar"
                assert account_columns["name"]["format_type"] == "character varying(80)"
                assert account_columns["name"]["not_null"] is True
                assert account_columns["name"]["column_default"] == "''::character varying"
                assert account_columns["email_confirmed_at"]["type_name"] == "timestamptz"
                assert (
                    account_columns["email_confirmed_at"]["format_type"]
                    == "timestamp with time zone"
                )
                assert account_columns["email_confirmed_at"]["not_null"] is False
                assert account_columns["email_confirmed_at"]["column_default"] is None
                assert _constraints(connection, "accounts") == {
                    ("p", "PRIMARY KEY (id)"),
                    ("u", "UNIQUE (email)"),
                }

                session_columns = _columns(connection, "sessions")
                assert tuple(session_columns) == SESSION_COLUMNS
                assert session_columns["id"]["type_name"] == "uuid"
                assert session_columns["id"]["format_type"] == "uuid"
                assert session_columns["id"]["not_null"] is True
                assert session_columns["account_id"]["type_name"] == "uuid"
                assert session_columns["account_id"]["format_type"] == "uuid"
                assert session_columns["account_id"]["not_null"] is True
                assert session_columns["account_id"]["column_default"] is None
                assert session_columns["last_request_at"]["type_name"] == "timestamptz"
                assert (
                    session_columns["last_request_at"]["format_type"]
                    == "timestamp with time zone"
                )
                assert session_columns["last_request_at"]["not_null"] is True
                assert session_columns["last_request_at"]["column_default"] is None
                assert _constraints(connection, "sessions") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (account_id) REFERENCES accounts(id)",
                    ),
                }

                link_columns = _columns(connection, "confirmation_links")
                assert tuple(link_columns) == CONFIRMATION_LINK_COLUMNS
                assert link_columns["id"]["type_name"] == "uuid"
                assert link_columns["id"]["format_type"] == "uuid"
                assert link_columns["id"]["not_null"] is True
                assert link_columns["id"]["column_default"] is None
                assert link_columns["account_id"]["type_name"] == "uuid"
                assert link_columns["account_id"]["format_type"] == "uuid"
                assert link_columns["account_id"]["not_null"] is True
                assert link_columns["account_id"]["column_default"] is None
                assert link_columns["token_hash"]["type_name"] == "text"
                assert link_columns["token_hash"]["format_type"] == "text"
                assert link_columns["token_hash"]["not_null"] is True
                assert link_columns["token_hash"]["column_default"] is None
                assert link_columns["expires_at"]["type_name"] == "timestamptz"
                assert (
                    link_columns["expires_at"]["format_type"]
                    == "timestamp with time zone"
                )
                assert link_columns["expires_at"]["not_null"] is True
                assert link_columns["expires_at"]["column_default"] is None
                assert link_columns["revoked_at"]["type_name"] == "timestamptz"
                assert (
                    link_columns["revoked_at"]["format_type"]
                    == "timestamp with time zone"
                )
                assert link_columns["revoked_at"]["not_null"] is False
                assert link_columns["revoked_at"]["column_default"] is None
                assert _constraints(connection, "confirmation_links") == {
                    ("p", "PRIMARY KEY (id)"),
                    ("u", "UNIQUE (token_hash)"),
                    (
                        "f",
                        "FOREIGN KEY (account_id) REFERENCES accounts(id)",
                    ),
                }

                period_columns = _columns(connection, "subscription_periods")
                assert tuple(period_columns) == SUBSCRIPTION_PERIOD_COLUMNS
                _assert_catalog_column(period_columns, "id", "uuid", "uuid", not_null=True)
                _assert_catalog_column(
                    period_columns, "account_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    period_columns,
                    "started_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                _assert_catalog_column(
                    period_columns,
                    "ends_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                _assert_catalog_column(
                    period_columns, "active", "bool", "boolean", not_null=True
                )
                assert _constraints(connection, "subscription_periods") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (account_id) REFERENCES accounts(id)",
                    ),
                }

                counter_columns = _columns(connection, "quota_counters")
                assert tuple(counter_columns) == QUOTA_COUNTER_COLUMNS
                _assert_catalog_column(
                    counter_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    counter_columns, "period_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    counter_columns, "used", "int4", "integer", not_null=True
                )
                assert _constraints(connection, "quota_counters") == {
                    ("p", "PRIMARY KEY (id)"),
                    ("u", "UNIQUE (period_id)"),
                    (
                        "f",
                        "FOREIGN KEY (period_id) REFERENCES subscription_periods(id)",
                    ),
                }

                charge_columns = _columns(connection, "quota_charges")
                assert tuple(charge_columns) == QUOTA_CHARGE_COLUMNS
                _assert_catalog_column(
                    charge_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    charge_columns, "period_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    charge_columns,
                    "created_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                assert _constraints(connection, "quota_charges") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (period_id) REFERENCES subscription_periods(id)",
                    ),
                }

                bot_columns = _columns(connection, "bots")
                assert tuple(bot_columns) == BOT_COLUMNS
                _assert_catalog_column(bot_columns, "id", "uuid", "uuid", not_null=True)
                _assert_catalog_column(
                    bot_columns, "account_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    bot_columns,
                    "name",
                    "varchar",
                    "character varying(80)",
                    not_null=True,
                )
                _assert_catalog_column(
                    bot_columns,
                    "color",
                    "varchar",
                    "character varying(7)",
                    not_null=True,
                )
                _assert_catalog_column(
                    bot_columns,
                    "icon",
                    "varchar",
                    "character varying(32)",
                    not_null=True,
                )
                _assert_catalog_column(
                    bot_columns,
                    "description",
                    "varchar",
                    "character varying(280)",
                    not_null=True,
                )
                _assert_catalog_column(
                    bot_columns,
                    "scenario",
                    "varchar",
                    "character varying(16000)",
                    not_null=True,
                )
                _assert_catalog_column(
                    bot_columns,
                    "updated_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                assert _constraints(connection, "bots") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (account_id) REFERENCES accounts(id)",
                    ),
                }

                chat_columns = _columns(connection, "chats")
                assert tuple(chat_columns) == CHAT_COLUMNS
                _assert_catalog_column(chat_columns, "id", "uuid", "uuid", not_null=True)
                _assert_catalog_column(
                    chat_columns, "bot_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    chat_columns,
                    "updated_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                assert _constraints(connection, "chats") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (bot_id) REFERENCES bots(id)",
                    ),
                }

                message_columns = _columns(connection, "messages")
                assert tuple(message_columns) == MESSAGE_COLUMNS
                _assert_catalog_column(
                    message_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    message_columns, "chat_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    message_columns,
                    "role",
                    "varchar",
                    "character varying(16)",
                    not_null=True,
                )
                _assert_catalog_column(
                    message_columns, "text", "text", "text", not_null=True
                )
                _assert_catalog_column(
                    message_columns, "code", "text", "text", not_null=False
                )
                _assert_catalog_column(
                    message_columns,
                    "updated_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                assert _constraints(connection, "messages") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (chat_id) REFERENCES chats(id)",
                    ),
                    ("c", MESSAGE_ROLE_CHECK),
                }

                desktop_columns = _columns(connection, "desktops")
                assert tuple(desktop_columns) == DESKTOP_COLUMNS
                _assert_catalog_column(
                    desktop_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    desktop_columns, "account_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    desktop_columns, "computer_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    desktop_columns, "working", "bool", "boolean", not_null=True
                )
                _assert_catalog_column(
                    desktop_columns,
                    "created_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                _assert_catalog_column(
                    desktop_columns, "timezone", "text", "text", not_null=False
                )
                _assert_catalog_column(
                    desktop_columns,
                    "last_pulse_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=False,
                )
                _assert_catalog_column(
                    desktop_columns, "session_id", "uuid", "uuid", not_null=False
                )
                assert desktop_columns["routines_stopped"]["type_name"] == "bool"
                assert desktop_columns["routines_stopped"]["format_type"] == "boolean"
                assert desktop_columns["routines_stopped"]["not_null"] is True
                assert desktop_columns["routines_stopped"]["column_default"] == "false"
                assert _constraints(connection, "desktops") == {
                    ("p", "PRIMARY KEY (id)"),
                    ("u", "UNIQUE (account_id, computer_id)"),
                    (
                        "f",
                        "FOREIGN KEY (account_id) REFERENCES accounts(id)",
                    ),
                }
                partial_indexes = [
                    row
                    for row in _indexes(connection, "desktops")
                    if row["index_name"] == "desktops_one_working"
                ]
                assert len(partial_indexes) == 1
                partial_row = partial_indexes[0]
                assert partial_row["is_unique"] is True
                assert partial_row["predicate"] == "working"
                assert list(partial_row["columns"]) == ["account_id"]
                routine_columns = _columns(connection, "routines")
                assert tuple(routine_columns) == ROUTINE_COLUMNS
                _assert_catalog_column(
                    routine_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    routine_columns, "bot_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    routine_columns,
                    "weekdays",
                    "_int4",
                    "integer[]",
                    not_null=True,
                )
                _assert_catalog_column(
                    routine_columns,
                    "time_of_day",
                    "time",
                    "time without time zone",
                    not_null=True,
                )
                assert routine_columns["paused"]["type_name"] == "bool"
                assert routine_columns["paused"]["format_type"] == "boolean"
                assert routine_columns["paused"]["not_null"] is True
                assert routine_columns["paused"]["column_default"] == "false"
                assert _constraints(connection, "routines") == {
                    ("p", "PRIMARY KEY (id)"),
                    (
                        "f",
                        "FOREIGN KEY (bot_id) REFERENCES bots(id)",
                    ),
                }
                run_columns = _columns(connection, "routine_runs")
                assert tuple(run_columns) == ROUTINE_RUN_COLUMNS
                _assert_catalog_column(
                    run_columns, "id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    run_columns, "routine_id", "uuid", "uuid", not_null=True
                )
                _assert_catalog_column(
                    run_columns, "local_date", "date", "date", not_null=True
                )
                _assert_catalog_column(
                    run_columns,
                    "due_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=True,
                )
                _assert_catalog_column(
                    run_columns,
                    "status",
                    "varchar",
                    "character varying(16)",
                    not_null=True,
                )
                _assert_catalog_column(
                    run_columns,
                    "claimed_at",
                    "timestamptz",
                    "timestamp with time zone",
                    not_null=False,
                )
                assert _constraints(connection, "routine_runs") == {
                    ("p", "PRIMARY KEY (id)"),
                    ("u", "UNIQUE (routine_id, local_date)"),
                    (
                        "f",
                        "FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE",
                    ),
                    ("c", ROUTINE_RUN_STATUS_CHECK),
                }
                try:
                    _insert_accepted_message_roles(connection)
                finally:
                    connection.rollback()
                for table_name in (
                    "accounts",
                    "sessions",
                    "confirmation_links",
                    "subscription_periods",
                    "quota_counters",
                    "quota_charges",
                    "bots",
                    "chats",
                    "messages",
                    "desktops",
                    "routines",
                    "routine_runs",
                ):
                    assert (
                        connection.execute(
                            text(f"SELECT count(*) FROM {table_name}")
                        ).scalar_one()
                        == 0
                    )
        finally:
            engine.dispose()
    finally:
        _compose("down", "--remove-orphans", timeout=60)
