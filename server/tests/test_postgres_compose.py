import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
DATABASE_SERVICE = "postgres"
APP_SERVICE_NAMES = {"app", "api", "server", "web", "fastapi"}


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


def _compose_config() -> dict:
    result = _compose("config", "--format", "json", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _environment(service: dict) -> dict[str, str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    values: dict[str, str] = {}
    for item in environment:
        key, separator, value = str(item).partition("=")
        assert separator == "="
        values[key] = value
    return values


def test_compose_has_one_database_and_no_application() -> None:
    config = _compose_config()
    services = config["services"]
    assert set(services) == {DATABASE_SERVICE}
    assert APP_SERVICE_NAMES.isdisjoint(services)

    database = services[DATABASE_SERVICE]
    image = database["image"]
    assert image == "postgres:17" or image.endswith("/postgres:17")
    assert "build" not in database

    published = [
        port
        for port in database["ports"]
        if port.get("target") == 5432 and port.get("protocol", "tcp") == "tcp"
    ]
    assert len(published) == 1
    assert published[0].get("host_ip") == "127.0.0.1"
    assert str(published[0]["published"]) == "5432"

    environment = _environment(database)
    assert environment["POSTGRES_USER"] == "neurobots"
    assert environment["POSTGRES_DB"] == "neurobots"
    assert environment["POSTGRES_HOST_AUTH_METHOD"] == "trust"
    assert "POSTGRES_PASSWORD" not in environment
    assert "POSTGRES_PASSWORD_FILE" not in environment


def test_postgres_accepts_readiness_check() -> None:
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout

        running = _compose("ps", "--services", timeout=30)
        assert running.returncode == 0, running.stderr
        assert running.stdout.split() == [DATABASE_SERVICE]

        ready = _compose(
            "exec",
            "-T",
            DATABASE_SERVICE,
            "pg_isready",
            "-U",
            "neurobots",
            "-d",
            "neurobots",
            timeout=30,
        )
        assert ready.returncode == 0, ready.stderr + ready.stdout
        assert "accepting connections" in ready.stdout
    finally:
        _compose("down", "--remove-orphans", timeout=60)
