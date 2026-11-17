import hashlib
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from server.db import create_db_engine
from server.models import accounts, confirmation_links, sessions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ACCEPTED = "abcdef1!"
REJECTED_PASSWORD = "abcdefgh1"
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


def _post(
    client: httpx.Client,
    origin: str,
    path: str,
    payload: object,
    send_origin: str | None,
):
    headers = {}
    if send_origin is not None:
        headers["Origin"] = send_origin
    return client.post(f"{origin}{path}", json=payload, headers=headers)


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _link_ids() -> set[uuid.UUID]:
    return {row["id"] for row in _rows(confirmation_links)}


def _confirmation_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if CONFIRM_PATH in line]


def test_register_stores_one_link_and_logs_its_url(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    raw_email = "  User Name@Example.COM  "
    stored_email = "user name@example.com"
    before = journal.text()
    with httpx.Client(timeout=5) as client:
        response = _post(
            client,
            origin,
            "/api/register",
            {"email": raw_email, "password": ACCEPTED},
            origin,
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        me = client.get(f"{origin}/api/me")
    body = response.json()
    assert body["email_confirmed"] is False
    assert body["period"] is None
    assert body["quota"] is None
    assert me.status_code == 200
    assert me.json()["email_confirmed"] is False
    assert me.json()["period"] is None
    assert me.json()["quota"] is None
    assert me.json()["account"]["email"] == stored_email

    account_id = uuid.UUID(body["account"]["id"])
    links = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(links) == 1
    link = links[0]
    assert tuple(link) == (
        "id",
        "account_id",
        "token_hash",
        "expires_at",
        "revoked_at",
    )
    assert link["revoked_at"] is None
    assert link["expires_at"].tzinfo is not None
    assert link["expires_at"].utcoffset() == timedelta(0)
    deadline = datetime.now(timezone.utc) + timedelta(hours=24)
    assert abs((link["expires_at"] - deadline).total_seconds()) < 10

    output = _wait_until(
        journal,
        lambda text: len(_confirmation_lines(text[len(before) :])) >= 1,
    )
    new = output[len(before) :]
    lines = _confirmation_lines(new)
    assert len(lines) == 1
    matched = re.fullmatch(
        rf"{re.escape(origin)}{re.escape(CONFIRM_PATH)}([^ ]+) {re.escape(stored_email)}",
        lines[0],
    )
    assert matched is not None
    token = matched.group(1)
    assert lines[0] == f"{origin}{CONFIRM_PATH}{token} {stored_email}"
    assert new.count(token) == 1
    assert link["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert link["token_hash"] != token
    stored = [link]
    stored.extend(row for row in _rows(accounts) if row["id"] == account_id)
    stored.extend(row for row in _rows(sessions) if row["account_id"] == account_id)
    visible = "\n".join(str(value) for row in stored for value in row.values())
    assert token not in visible
    assert token not in response.text
    assert token not in cookie
    assert token not in me.text
    assert ACCEPTED not in output


def test_failed_register_does_not_store_a_link_or_log_the_password(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    before_ids = _link_ids()
    before = journal.text()
    with httpx.Client(timeout=5) as client:
        missing_origin = _post(
            client,
            origin,
            "/api/register",
            {"email": "origin-miss@example.com", "password": ACCEPTED},
            None,
        )
        foreign_origin = _post(
            client,
            origin,
            "/api/register",
            {"email": "origin-bad@example.com", "password": ACCEPTED},
            "http://evil.example",
        )
        bad_password = _post(
            client,
            origin,
            "/api/register",
            {"email": "bad-password@example.com", "password": REJECTED_PASSWORD},
            origin,
        )
        invalid_body = _post(
            client,
            origin,
            "/api/register",
            {"email": "invalid-body@example.com"},
            origin,
        )
        not_strings = _post(
            client,
            origin,
            "/api/register",
            {"email": "not-strings@example.com", "password": 1},
            origin,
        )
        not_object = _post(
            client,
            origin,
            "/api/register",
            ["not-object@example.com", ACCEPTED],
            origin,
        )
    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403
    assert bad_password.status_code == 400
    assert invalid_body.status_code == 400
    assert not_strings.status_code == 400
    assert not_object.status_code == 400
    output = _wait_until(
        journal,
        lambda text: text[len(before) :].count("POST /api/register HTTP/1.1") >= 6,
    )
    new = output[len(before) :]
    assert _confirmation_lines(new) == []
    assert _link_ids() == before_ids
    assert ACCEPTED not in new
    assert REJECTED_PASSWORD not in new

    before_taken = journal.text()
    with httpx.Client(timeout=5) as client:
        created = _post(
            client,
            origin,
            "/api/register",
            {"email": "  Taken Link@Example.COM ", "password": ACCEPTED},
            origin,
        )
        assert created.status_code == 200
        _wait_until(
            journal,
            lambda text: (
                len(_confirmation_lines(text[len(before_taken) :])) >= 1
                and text[len(before_taken) :].count("POST /api/register HTTP/1.1") >= 1
            ),
        )
        marked = journal.text()
        taken = _post(
            client,
            origin,
            "/api/register",
            {"email": "taken link@example.com", "password": ACCEPTED},
            origin,
        )
    assert taken.status_code == 400
    assert taken.json()["code"] == "email_taken"
    output = _wait_until(
        journal,
        lambda text: text[len(marked) :].count("POST /api/register HTTP/1.1") >= 1,
    )
    account_id = uuid.UUID(created.json()["account"]["id"])
    owned = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(owned) == 1
    assert _confirmation_lines(output[len(marked) :]) == []
    assert ACCEPTED not in output[len(marked) :]
    assert ACCEPTED not in output


def test_login_does_not_issue_a_confirmation_link(
    running_server: tuple[str, Journal],
) -> None:
    origin, journal = running_server
    before = journal.text()
    with httpx.Client(timeout=5) as client:
        registered = _post(
            client,
            origin,
            "/api/register",
            {"email": "  Login Link@Example.COM ", "password": ACCEPTED},
            origin,
        )
        assert registered.status_code == 200
        _wait_until(
            journal,
            lambda text: (
                len(_confirmation_lines(text[len(before) :])) >= 1
                and text[len(before) :].count("POST /api/register HTTP/1.1") >= 1
            ),
        )
        marked = journal.text()
        logged_in = _post(
            client,
            origin,
            "/api/login",
            {"email": "login link@example.com", "password": ACCEPTED},
            origin,
        )
    assert logged_in.status_code == 200
    assert logged_in.json()["email_confirmed"] is False
    assert logged_in.json()["period"] is None
    assert logged_in.json()["quota"] is None
    output = _wait_until(
        journal,
        lambda text: text[len(marked) :].count("POST /api/login HTTP/1.1") >= 1,
    )
    account_id = uuid.UUID(registered.json()["account"]["id"])
    owned = [row for row in _rows(confirmation_links) if row["account_id"] == account_id]
    assert len(owned) == 1
    assert _confirmation_lines(output[len(marked) :]) == []
    assert ACCEPTED not in output[len(marked) :]
    assert ACCEPTED not in output
