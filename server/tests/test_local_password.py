import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import http.client
import httpx
import pytest
from sqlalchemy import text

from server.db import create_db_engine

REPO = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
sys.path.insert(0, str(REPO / "desktop" / "src"))

from desktop.keychain import SECURITY_PATH  # noqa: E402
from desktop.password_screen import LOOPBACK, serve_password_screen  # noqa: E402

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-vault-9"


class _Security:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        assert command[0] == SECURITY_PATH
        operation = command[1]
        service = command[command.index("-s") + 1]
        account = command[command.index("-a") + 1]
        if operation == "add-generic-password":
            self.items[(service, account)] = command[command.index("-w") + 1]
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == "find-generic-password":
            stored = self.items.get((service, account))
            if stored is None:
                return subprocess.CompletedProcess(command, 44, "", "")
            return subprocess.CompletedProcess(command, 0, stored + "\n", "")
        raise AssertionError(operation)


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


@pytest.fixture(scope="module")
def database() -> None:
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
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
    try:
        yield
    finally:
        _compose("down", "--remove-orphans", timeout=60)


class _Journal:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        assert process.stdout is not None
        self._thread = threading.Thread(target=self._read, args=(process.stdout,), daemon=True)
        self._thread.start()

    def _read(self, stream) -> None:
        for line in stream:
            with self._lock:
                self._chunks.append(line)

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def join(self) -> None:
        self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.fixture(scope="module")
def running_server(database: None):
    port = _free_port()
    origin = f"http://{LOOPBACK}:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            LOOPBACK,
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    journal = _Journal(process)
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=5) as client:
            while time.monotonic() < deadline:
                if "Application startup complete." in journal.text() and _health_ok(client, origin):
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            assert "Application startup complete." in journal.text(), journal.text()
            assert _health_ok(client, origin)
        yield origin, journal
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        journal.join()


def test_local_password_stays_out_of_the_database_journal_and_cabinet(
    running_server: tuple[str, _Journal],
) -> None:
    origin, journal = running_server
    cabinet_port = int(origin.rsplit(":", 1)[1])
    before_journal = journal.text()
    before_counts = _counts()
    assert SECRET not in before_journal
    security = _Security()
    opened = serve_password_screen(run=security)
    real_connect = socket.socket.connect
    try:
        def connect(self, address):
            assert isinstance(address, tuple) and len(address) >= 2, address
            host, port = address[0], address[1]
            assert host == LOOPBACK, address
            assert port != cabinet_port, address
            assert port == opened.port, address
            return real_connect(self, address)

        socket.socket.connect = connect
        try:
            posted, posted_body, posted_headers = _post(opened, SITE, LOGIN, SECRET)
            assert posted == 303
            assert SECRET.encode() not in posted_body
            assert _header(posted_headers, "Location") == "/?saved=1"
            read_status, read_body, _read_headers = _exchange(
                opened,
                "GET",
                f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
            )
            assert read_status == 200
            assert read_body.decode() == SECRET
            refused, refused_body, _refused_headers = _exchange(
                opened,
                "GET",
                f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
                headers={
                    "Origin": origin,
                    "Sec-Fetch-Site": "same-site",
                },
            )
            assert refused == 403
            assert SECRET.encode() not in refused_body
        finally:
            socket.socket.connect = real_connect
        time.sleep(0.3)
        delta = journal.text()[len(before_journal) :]
        assert SECRET not in journal.text()
        assert SECRET not in delta
        assert "HTTP/1.1" not in delta
        assert _counts() == before_counts
        assert SECRET not in _database_text()
        assert security.commands[0][1] == "add-generic-password"
        assert security.commands[1][1] == "find-generic-password"
        screen_source = (REPO / "desktop" / "src" / "desktop" / "password_screen.py").read_text(
            encoding="utf-8"
        )
        keychain_source = (REPO / "desktop" / "src" / "desktop" / "keychain.py").read_text(
            encoding="utf-8"
        )
        combined = screen_source + keychain_source
        assert "psycopg" not in combined
        assert "sqlalchemy" not in combined
        assert "urllib.request" not in combined
        assert "httpx" not in combined
    finally:
        opened.close()


def _counts() -> dict[str, int]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            names = connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
            ).scalars()
            return {
                name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
                for name in names
            }
    finally:
        engine.dispose()


def _database_text() -> str:
    engine = create_db_engine()
    chunks: list[str] = []
    try:
        with engine.connect() as connection:
            names = connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
            ).scalars()
            for name in names:
                rows = connection.execute(text(f'SELECT * FROM "{name}"')).mappings().all()
                for row in rows:
                    chunks.extend(str(value) for value in row.values())
    finally:
        engine.dispose()
    return "\n".join(chunks)


def _post(opened, site: str, login: str, password: str):
    body = urlencode({"site": site, "login": login, "password": password}).encode()
    return _exchange(
        opened,
        "POST",
        "/passwords",
        body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        },
    )


def _exchange(opened, method: str, path: str, body: bytes | None = None, headers=None):
    connection = http.client.HTTPConnection(LOOPBACK, opened.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        connection.close()


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return None
