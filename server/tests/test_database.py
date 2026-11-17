import ast
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version
from sqlalchemy import text

from server.db import DATABASE_URL, create_db_engine, database_responds, metadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"


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


def _project_dependencies() -> list[Requirement]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        dependencies = tomllib.load(project_file)["project"]["dependencies"]
    return [Requirement(dependency) for dependency in dependencies]


def _requirement(name: str) -> Requirement:
    matches = [
        requirement
        for requirement in _project_dependencies()
        if requirement.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_sqlalchemy_and_psycopg_meet_the_stack() -> None:
    sqlalchemy_req = _requirement("sqlalchemy")
    psycopg_req = _requirement("psycopg")
    assert _requirement("alembic").name == "alembic"
    assert Version("2.0.52") in sqlalchemy_req.specifier
    assert Version("2.0.51") not in sqlalchemy_req.specifier
    assert Version("2.1.0") not in sqlalchemy_req.specifier
    assert Version("2.1.9") not in sqlalchemy_req.specifier
    assert Version("3.0.0") in psycopg_req.specifier
    assert Version("2.9.11") not in psycopg_req.specifier
    installed_sqlalchemy = Version(version("sqlalchemy"))
    assert installed_sqlalchemy >= Version("2.0.52")
    assert installed_sqlalchemy < Version("2.1")
    assert Version(version("psycopg")) >= Version("3")


def test_alembic_uses_the_same_database_and_metadata() -> None:
    ini_url = ""
    for line in (PROJECT_ROOT / "alembic.ini").read_text().splitlines():
        if line.startswith("sqlalchemy.url"):
            ini_url = line.split("=", 1)[1].strip()
    assert ini_url == DATABASE_URL
    assert ":" not in DATABASE_URL.split("@", 1)[0].split("//", 1)[1]

    module = ast.parse((PROJECT_ROOT / "alembic" / "env.py").read_text())
    imported: set[str] = set()
    assigned: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "server.db":
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Name):
                assigned[target.id] = node.value.id
    assert {"DATABASE_URL", "metadata"} <= imported
    assert assigned["target_metadata"] == "metadata"
    assert metadata.tables == {}
    assert list(VERSIONS_DIR.glob("*.py")) == []


def test_select_one_and_alembic_upgrade_head() -> None:
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout

        assert database_responds() is True
        engine = create_db_engine()
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            engine.dispose()

        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout
        assert "Running upgrade" not in upgrade.stderr
        assert "Running upgrade" not in upgrade.stdout
        assert list(VERSIONS_DIR.glob("*.py")) == []

        engine = create_db_engine()
        try:
            with engine.connect() as connection:
                tables = connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                ).scalars().all()
        finally:
            engine.dispose()
        assert set(tables) <= {"alembic_version"}
    finally:
        _compose("down", "--remove-orphans", timeout=60)
