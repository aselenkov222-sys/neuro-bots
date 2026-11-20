import logging
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app import app
from server.confirmation import confirmation_letter, hash_token, log_confirmation_link
from server.db import create_db_engine
from server.models import accounts, confirmation_links

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
PASSWORD = "abcdef1!"
CONFIRM_PATH = "/api/confirm-email?token="
ORIGIN = "http://testserver"
EXPIRED = "Ссылка больше не действует"
TARIFF_REFUSAL = {
    "email_confirmed": False,
    "destination": "tariff",
    "message": EXPIRED,
}


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


@contextmanager
def _captured_journal() -> Iterator[list[str]]:
    found: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(record.getMessage())

    handler = _Capture()
    handler.setLevel(logging.INFO)
    target = logging.getLogger("server.confirmation")
    target.addHandler(handler)
    try:
        yield found
    finally:
        target.removeHandler(handler)


def _letter_token(line: str, email: str) -> str:
    matched = re.fullmatch(
        rf"{re.escape(ORIGIN)}{re.escape(CONFIRM_PATH)}([^ ]+) {re.escape(email)}",
        line,
    )
    assert matched is not None
    return matched.group(1)


def _links(account_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(confirmation_links).where(
                        confirmation_links.c.account_id == account_id
                    )
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _account(account_id: uuid.UUID) -> dict:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(select(accounts).where(accounts.c.id == account_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    return dict(row)


def test_configured_sink_receives_the_register_letter(
    database: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "mail"
    monkeypatch.setenv("NEURO_BOTS_MAIL_SINK", str(sink))
    raw_email = "  Sink User@Example.COM  "
    stored_email = "sink user@example.com"
    with _captured_journal() as journal:
        with TestClient(app) as client:
            response = client.post(
                "/api/register",
                json={"email": raw_email, "password": PASSWORD},
                headers={"Origin": ORIGIN},
            )
    assert response.status_code == 200
    assert response.json()["account"]["email"] == stored_email
    text = sink.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 1
    token = _letter_token(lines[0], stored_email)
    letter = confirmation_letter(f"{ORIGIN}/", token, stored_email)
    assert text == f"{letter}\n"
    assert journal == [letter]
    assert PASSWORD not in text


def test_resend_appends_a_letter_and_revokes_the_previous_link(
    database: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "mail"
    monkeypatch.setenv("NEURO_BOTS_MAIL_SINK", str(sink))
    raw_email = "  Resend Sink@Example.COM  "
    stored_email = "resend sink@example.com"
    with _captured_journal() as journal:
        with TestClient(app) as client:
            registered = client.post(
                "/api/register",
                json={"email": raw_email, "password": PASSWORD},
                headers={"Origin": ORIGIN},
            )
            assert registered.status_code == 200
            assert registered.json()["account"]["email"] == stored_email
            account_id = uuid.UUID(registered.json()["account"]["id"])
            first = sink.read_text(encoding="utf-8")
            old_token = _letter_token(first.removesuffix("\n"), stored_email)
            replaced = client.post("/api/confirmation", headers={"Origin": ORIGIN})
            assert replaced.status_code == 200
            text = sink.read_text(encoding="utf-8")
            lines = text.splitlines()
            assert len(lines) == 2
            assert lines[0] == first.removesuffix("\n")
            new_token = _letter_token(lines[1], stored_email)
            assert new_token != old_token
            new_letter = confirmation_letter(f"{ORIGIN}/", new_token, stored_email)
            assert text == f"{lines[0]}\n{new_letter}\n"
            assert PASSWORD not in text
            links = _links(account_id)
            assert len(links) == 2
            old_link = next(
                row for row in links if row["token_hash"] == hash_token(old_token)
            )
            new_link = next(
                row for row in links if row["token_hash"] == hash_token(new_token)
            )
            assert old_link["revoked_at"] is not None
            assert new_link["revoked_at"] is None
            refused = client.get("/api/confirm-email", params={"token": old_token})
            assert refused.status_code == 200
            assert refused.json() == TARIFF_REFUSAL
            assert _account(account_id)["email_confirmed_at"] is None
            opened = client.get("/api/confirm-email", params={"token": new_token})
            assert opened.status_code == 200
            assert opened.json() == {"email_confirmed": True, "destination": "bots"}
    assert _account(account_id)["email_confirmed_at"] is not None
    assert journal == [lines[0], new_letter]


def test_unset_sink_delivers_without_writing_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEURO_BOTS_MAIL_SINK", raising=False)

    def opened(*args, **kwargs):
        raise AssertionError("configured sink was opened")

    monkeypatch.setattr("server.confirmation.open", opened, raising=False)
    email = "unset-sink@example.com"
    with _captured_journal() as journal:
        log_confirmation_link(f"{ORIGIN}/", "x", email)
    assert journal == [confirmation_letter(f"{ORIGIN}/", "x", email)]

    monkeypatch.setenv("NEURO_BOTS_MAIL_SINK", "")
    with _captured_journal() as journal:
        log_confirmation_link(f"{ORIGIN}/", "x", email)
    assert journal == [confirmation_letter(f"{ORIGIN}/", "x", email)]
