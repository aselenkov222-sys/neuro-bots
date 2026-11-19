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


def _revision_chain() -> list[tuple[str, str | None]]:
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
            raise AssertionError(f"{script.name} revision id is missing")
        links[revision] = down_revision
    roots = [revision for revision, down in links.items() if down is None]
    assert roots == ["3c87a0f84d5e"]
    ordered = [roots[0]]
    while True:
        children = [
            revision for revision, down in links.items() if down == ordered[-1]
        ]
        if not children:
            break
        assert len(children) == 1
        ordered.append(children[0])
    assert set(ordered) == set(links)
    return [(revision, links[revision]) for revision in ordered]


def test_alembic_uses_the_same_database_and_metadata() -> None:
    ini_url = ""
    for line in (PROJECT_ROOT / "alembic.ini").read_text().splitlines():
        if line.startswith("sqlalchemy.url"):
            ini_url = line.split("=", 1)[1].strip()
    assert ini_url == DATABASE_URL
    assert ":" not in DATABASE_URL.split("@", 1)[0].split("//", 1)[1]

    module = ast.parse((PROJECT_ROOT / "alembic" / "env.py").read_text())
    imported: set[str] = set()
    imported_modules: set[str] = set()
    assigned: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "server.db":
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Name):
                assigned[target.id] = node.value.id
    assert {"DATABASE_URL", "metadata"} <= imported
    assert assigned["target_metadata"] == "metadata"
    assert "server.models" in imported_modules
    import server.models

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
        "routine_checks",
        "desktop_notices",
        "dangerous_steps",
    }
    assert server.models.accounts.metadata is metadata
    assert server.models.sessions.metadata is metadata
    assert server.models.confirmation_links.metadata is metadata
    assert server.models.subscription_periods.metadata is metadata
    assert server.models.quota_counters.metadata is metadata
    assert server.models.quota_charges.metadata is metadata
    assert server.models.bots.metadata is metadata
    assert server.models.chats.metadata is metadata
    assert server.models.messages.metadata is metadata
    assert server.models.desktops.metadata is metadata
    assert server.models.routines.metadata is metadata
    assert server.models.routine_runs.metadata is metadata
    assert server.models.routine_checks.metadata is metadata
    assert server.models.desktop_notices.metadata is metadata
    assert server.models.dangerous_steps.metadata is metadata
    chain = _revision_chain()
    assert chain[0] == ("3c87a0f84d5e", None)
    assert ("ff582d8bc7fb", "3c87a0f84d5e") in chain
    assert ("048ee727aa3e", "ff582d8bc7fb") in chain
    assert ("c54ff0c6fb42", "048ee727aa3e") in chain
    assert ("78372be7d547", "c54ff0c6fb42") in chain
    assert ("f524099440d0", "78372be7d547") in chain
    assert ("b7e2c4a91d58", "f524099440d0") in chain
    assert ("a4f0c8e21b67", "b7e2c4a91d58") in chain
    assert ("9e4b1c7a6d20", "a4f0c8e21b67") in chain
    assert ("f7a2c6e81d04", "9e4b1c7a6d20") in chain
    assert ("a1c4e8b27d60", "f7a2c6e81d04") in chain
    assert ("d4f1b8e06c32", "a1c4e8b27d60") in chain
    assert ("b8e5c2a94f17", "d4f1b8e06c32") in chain
    assert ("c4d8e2a71b06", "b8e5c2a94f17") in chain
    assert ("e6b1a9c34f28", "c4d8e2a71b06") in chain
    assert ("f3a8c1d27e05", "e6b1a9c34f28") in chain
    assert ("b3c7e9a41d52", "f3a8c1d27e05") in chain
    assert chain[-1] == ("b3c7e9a41d52", "f3a8c1d27e05")
    assert chain[-1][1] == "f3a8c1d27e05"
    assert chain[-1][0] != "f3a8c1d27e05"
    assert chain[-1][0] != "e6b1a9c34f28"
    assert chain[-1][0] != "c4d8e2a71b06"
    assert chain[-1][0] != "d4f1b8e06c32"
    assert chain[-1][0] != "a1c4e8b27d60"
    assert chain[-1][0] != "f7a2c6e81d04"
    assert chain[-1][0] != "9e4b1c7a6d20"
    assert chain[-1][0] != "a4f0c8e21b67"
    assert chain[-1][0] != "b7e2c4a91d58"
    assert chain[-1][0] != "f524099440d0"
    assert chain[-1][0] != "78372be7d547"
    assert chain[-1][0] != "c54ff0c6fb42"
    assert chain[-1][0] != "048ee727aa3e"
    assert chain[-1][0] != "ff582d8bc7fb"


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
        chain = _revision_chain()
        assert chain[0] == ("3c87a0f84d5e", None)
        assert ("ff582d8bc7fb", "3c87a0f84d5e") in chain
        assert ("048ee727aa3e", "ff582d8bc7fb") in chain
        assert ("c54ff0c6fb42", "048ee727aa3e") in chain
        assert ("78372be7d547", "c54ff0c6fb42") in chain
        assert ("f524099440d0", "78372be7d547") in chain
        assert ("b7e2c4a91d58", "f524099440d0") in chain
        assert ("a4f0c8e21b67", "b7e2c4a91d58") in chain
        assert ("9e4b1c7a6d20", "a4f0c8e21b67") in chain
        assert ("f7a2c6e81d04", "9e4b1c7a6d20") in chain
        assert ("a1c4e8b27d60", "f7a2c6e81d04") in chain
        assert ("d4f1b8e06c32", "a1c4e8b27d60") in chain
        assert ("b8e5c2a94f17", "d4f1b8e06c32") in chain
        assert ("c4d8e2a71b06", "b8e5c2a94f17") in chain
        assert ("e6b1a9c34f28", "c4d8e2a71b06") in chain
        assert ("f3a8c1d27e05", "e6b1a9c34f28") in chain
        assert ("b3c7e9a41d52", "f3a8c1d27e05") in chain
        assert chain[-1] == ("b3c7e9a41d52", "f3a8c1d27e05")
        assert chain[-1][1] == "f3a8c1d27e05"
        assert chain[-1][0] != "f3a8c1d27e05"
        assert chain[-1][0] != "e6b1a9c34f28"
        assert chain[-1][0] != "c4d8e2a71b06"
        assert chain[-1][0] != "d4f1b8e06c32"
        assert chain[-1][0] != "a1c4e8b27d60"
        assert chain[-1][0] != "f7a2c6e81d04"
        assert chain[-1][0] != "9e4b1c7a6d20"
        assert chain[-1][0] != "a4f0c8e21b67"
        assert chain[-1][0] != "b7e2c4a91d58"
        assert chain[-1][0] != "f524099440d0"
        assert chain[-1][0] != "78372be7d547"
        assert chain[-1][0] != "c54ff0c6fb42"
        assert chain[-1][0] != "048ee727aa3e"
        assert chain[-1][0] != "ff582d8bc7fb"
        assert "Running upgrade" in upgrade.stderr + upgrade.stdout

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
            "routine_checks",
            "desktop_notices",
            "dangerous_steps",
            "alembic_version",
        }
    finally:
        _compose("down", "--remove-orphans", timeout=60)
