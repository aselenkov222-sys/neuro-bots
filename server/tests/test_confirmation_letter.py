import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from server.confirmation import confirmation_letter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
PASSWORD = "abcdef1!"
CONFIRM_PATH = "/api/confirm-email?token="


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


class Journal:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        assert process.stdout is not None
        self._thread = threading.Thread(
            target=self._read,
            args=(process.stdout,),
            daemon=True,
        )
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
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(process: subprocess.Popen[str], journal: Journal) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    journal.join()


def _wait_until(journal: Journal, predicate, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = journal.text()
        if predicate(current):
            return current
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    current = journal.text()
    assert predicate(current), current
    return current


@pytest.fixture(scope="module")
def running_server(database: None):
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    journal = Journal(process)
    try:
        with httpx.Client(timeout=5) as client:
            _wait_until(
                journal,
                lambda text: (
                    "Application startup complete." in text and _health_ok(client, origin)
                ),
                timeout=20,
            )
        yield origin, journal
    finally:
        _stop(process, journal)


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _confirmation_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if CONFIRM_PATH in line and line.startswith("http")
    ]


def test_journal_letter_is_absolute_url_and_normalized_email(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    raw_email = "  Letter User@Example.COM  "
    stored_email = "letter user@example.com"
    before = journal.text()
    with httpx.Client(timeout=5) as client:
        response = client.post(
            f"{origin}/api/register",
            json={"email": raw_email, "password": PASSWORD},
            headers={"Origin": origin},
        )
    assert response.status_code == 200
    assert response.json()["account"]["email"] == stored_email
    output = _wait_until(
        journal,
        lambda text: len(_confirmation_lines(text[len(before) :])) >= 1,
    )
    new = output[len(before) :]
    lines = _confirmation_lines(new)
    assert len(lines) == 1
    letter = lines[0]
    matched = re.fullmatch(
        rf"{re.escape(origin)}{re.escape(CONFIRM_PATH)}([^ ]+) {re.escape(stored_email)}",
        letter,
    )
    assert matched is not None
    token = matched.group(1)
    assert "\n" not in confirmation_letter(f"{origin}/", token, stored_email)
    assert letter == confirmation_letter(f"{origin}/", token, stored_email)
    assert PASSWORD not in letter
    assert PASSWORD not in new
