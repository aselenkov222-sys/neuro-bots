import ast
import subprocess
import sys
from pathlib import Path

from sqlalchemy import DateTime, String, Text, Uuid, text
from sqlalchemy.engine import Connection

from server.db import create_db_engine, metadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"

ACCOUNT_COLUMNS = ("id", "email", "password_hash", "name")
SESSION_COLUMNS = ("id", "account_id", "last_request_at")


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


def _revision_id() -> str:
    scripts = list(VERSIONS_DIR.glob("*.py"))
    assert len(scripts) == 1
    module = ast.parse(scripts[0].read_text())
    for node in module.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError("migration revision id is missing")


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


def test_upgrade_head_creates_accounts_and_sessions() -> None:
    import server.models

    accounts = metadata.tables["accounts"]
    sessions = metadata.tables["sessions"]
    assert set(metadata.tables) == {"accounts", "sessions"}
    assert accounts is server.models.accounts
    assert sessions is server.models.sessions
    assert tuple(accounts.columns.keys()) == ACCOUNT_COLUMNS
    assert tuple(sessions.columns.keys()) == SESSION_COLUMNS

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
                assert set(tables) == {"accounts", "sessions", "alembic_version"}
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
        finally:
            engine.dispose()
    finally:
        _compose("down", "--remove-orphans", timeout=60)
