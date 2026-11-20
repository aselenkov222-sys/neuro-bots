import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from server.accounts import create_account, create_session
from server.app import app
from server.bots import inbound_webhook_address, new_inbound_secret
from server.db import create_db_engine
from server.models import accounts, bots

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
PREVIOUS_REVISION = "b6d2e8a14c70"
CLIENT_ADDRESS = "/api/hooks/client-supplied-secret"
PATCHED_ADDRESS = "/api/hooks/patched-by-client"
DOCUMENT_FIELDS = (
    "id",
    "name",
    "color",
    "icon",
    "description",
    "scenario",
    "updated_at",
    "chief",
    "inbound_webhook",
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


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def database() -> None:
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
    assert up.returncode == 0, up.stderr + up.stdout
    upgrade = _alembic("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout
    try:
        yield
    finally:
        _compose("down", "--remove-orphans", timeout=60)


def _open_confirmed(email: str) -> tuple[str, uuid.UUID]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(
                    email_confirmed_at=datetime(2026, 11, 20, tzinfo=timezone.utc)
                )
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _create(client: TestClient, payload: dict) -> object:
    return client.post("/api/bots", json=payload, headers={"Origin": ORIGIN})


def _secrets() -> list[str]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            values = connection.execute(select(bots.c.inbound_secret)).scalars().all()
    finally:
        engine.dispose()
    return [str(value) for value in values]


def _stored_secret(bot_id: str) -> str:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            secret = connection.execute(
                select(bots.c.inbound_secret).where(bots.c.id == uuid.UUID(bot_id))
            ).scalar_one()
    finally:
        engine.dispose()
    return str(secret)


def _secret_of(address: str) -> str:
    secret = address.removeprefix("/api/hooks/")
    assert address == inbound_webhook_address(secret)
    assert secret != address
    sample = new_inbound_secret()
    assert re.fullmatch(rf"[A-Za-z0-9_-]{{{len(sample)}}}", secret) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", secret) is None
    return secret


def test_create_stores_a_server_secret_and_repeats_the_address(
    database: None,
) -> None:
    cookie, account_id = _open_confirmed("inbound-create@example.com")
    with _client(cookie) as client:
        created = _create(client, {"name": "Искра"})
        assert created.status_code == 200
        body = created.json()
        assert tuple(body) == DOCUMENT_FIELDS
        assert body["name"] == "Искра"
        assert body["color"] == "#c85a12"
        assert body["icon"] == "spark"
        assert body["description"] == ""
        assert body["scenario"] == ""
        assert body["chief"] is False
        secret = _secret_of(body["inbound_webhook"])
        assert _stored_secret(body["id"]) == secret
        assert secret != body["id"]
        again = client.get(f"/api/bots/{body['id']}")
        listed = client.get("/api/bots")
        second = _create(client, {"name": "Вторая"})
    assert again.status_code == 200
    assert again.json()["inbound_webhook"] == body["inbound_webhook"]
    assert again.json() == body
    assert listed.status_code == 200
    assert listed.json()["bots"][0]["inbound_webhook"] == body["inbound_webhook"]
    assert second.status_code == 200
    other = _secret_of(second.json()["inbound_webhook"])
    assert other != secret
    assert _stored_secret(second.json()["id"]) == other
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    bots.insert().values(
                        id=uuid.uuid4(),
                        account_id=account_id,
                        name="Копия",
                        color="#c85a12",
                        icon="spark",
                        description="",
                        scenario="",
                        updated_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
                        inbound_secret=secret,
                    )
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_client_secret_is_not_stored(database: None) -> None:
    cookie, _account_id = _open_confirmed("inbound-client@example.com")
    with _client(cookie) as client:
        created = _create(
            client,
            {
                "name": "Искра",
                "inbound_webhook": CLIENT_ADDRESS,
                "inbound_secret": "client-supplied-secret",
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert tuple(body) == DOCUMENT_FIELDS
        assert body["inbound_webhook"] != CLIENT_ADDRESS
        secret = _secret_of(body["inbound_webhook"])
        assert secret != "client-supplied-secret"
        assert _stored_secret(body["id"]) == secret
        same_client = _create(
            client,
            {"name": "Ещё", "inbound_webhook": CLIENT_ADDRESS},
        )
        assert same_client.status_code == 200
        assert same_client.json()["inbound_webhook"] != CLIENT_ADDRESS
        assert same_client.json()["inbound_webhook"] != body["inbound_webhook"]
        patched = client.patch(
            f"/api/bots/{body['id']}",
            json={
                "name": "После",
                "inbound_webhook": PATCHED_ADDRESS,
                "inbound_secret": "patched-by-client",
            },
            headers={"Origin": ORIGIN},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "После"
        assert patched.json()["inbound_webhook"] == body["inbound_webhook"]
        assert _stored_secret(body["id"]) == secret
        only_secret = client.patch(
            f"/api/bots/{body['id']}",
            json={"inbound_webhook": CLIENT_ADDRESS},
            headers={"Origin": ORIGIN},
        )
        card = client.get(f"/api/bots/{body['id']}")
    assert only_secret.status_code == 200
    assert only_secret.json()["name"] == "После"
    assert only_secret.json()["inbound_webhook"] == body["inbound_webhook"]
    assert card.status_code == 200
    assert card.json()["inbound_webhook"] == body["inbound_webhook"]
    assert _stored_secret(body["id"]) == secret


def test_deleting_the_bot_or_the_account_removes_the_address(database: None) -> None:
    owner_cookie, _owner_id = _open_confirmed("inbound-delete@example.com")
    other_cookie, _other_id = _open_confirmed("inbound-kept@example.com")
    with _client(owner_cookie) as owner, _client(other_cookie) as other:
        first = _create(owner, {"name": "Первый"})
        second = _create(owner, {"name": "Второй"})
        kept = _create(other, {"name": "Чужой"})
        assert first.status_code == second.status_code == kept.status_code == 200
        first_secret = _secret_of(first.json()["inbound_webhook"])
        second_secret = _secret_of(second.json()["inbound_webhook"])
        kept_secret = _secret_of(kept.json()["inbound_webhook"])
        removed = owner.delete(
            f"/api/bots/{first.json()['id']}",
            headers={"Origin": ORIGIN},
        )
        gone = owner.get(f"/api/bots/{first.json()['id']}")
        still = owner.get(f"/api/bots/{second.json()['id']}")
        assert removed.status_code == 204
        assert gone.status_code == 404
        assert still.status_code == 200
        assert still.json()["inbound_webhook"] == second.json()["inbound_webhook"]
        remaining = _secrets()
        assert first_secret not in remaining
        assert second_secret in remaining
        assert kept_secret in remaining
        deleted = owner.request(
            "DELETE",
            "/api/me",
            json={"confirm": True},
            headers={"Origin": ORIGIN},
        )
        hidden = other.get(f"/api/bots/{second.json()['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {}
    assert hidden.status_code == 404
    after = _secrets()
    assert first_secret not in after
    assert second_secret not in after
    assert kept_secret in after
    assert _stored_secret(kept.json()["id"]) == kept_secret


class _Journal:
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


def _stop(process: subprocess.Popen[str], journal: _Journal) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    journal.join()


def _wait_until(journal: _Journal, predicate, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = journal.text()
        if predicate(current):
            return current
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    return journal.text()


def test_create_does_not_write_the_address_to_the_server_journal(
    database: None,
) -> None:
    cookie, _account_id = _open_confirmed("inbound-journal@example.com")
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
    journal = _Journal(process)
    try:
        with httpx.Client(timeout=5) as client:
            ready = _wait_until(
                journal,
                lambda current: "Application startup complete." in current
                and _health_ok(client, origin),
            )
            assert "Application startup complete." in ready, ready
            client.cookies.set("session", cookie)
            created = client.post(
                f"{origin}/api/bots",
                json={
                    "name": "Журнал",
                    "inbound_webhook": CLIENT_ADDRESS,
                },
                headers={"Origin": origin},
            )
        assert created.status_code == 200, created.text
        address = created.json()["inbound_webhook"]
        secret = _secret_of(address)
        assert address != CLIENT_ADDRESS
        assert _stored_secret(created.json()["id"]) == secret
        written = _wait_until(journal, lambda current: "POST /api/bots" in current)
        assert "POST /api/bots" in written
        assert address not in written
        assert secret not in written
        assert "/api/hooks/" not in written
        assert "client-supplied-secret" not in written
    finally:
        _stop(process, journal)


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _bot_columns(connection) -> list[str]:
    return list(
        connection.execute(
            text(
                """
                SELECT a.attname
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = 'bots'
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum
                """
            )
        ).scalars()
    )


def _insert_old_bot(connection, account_id: uuid.UUID, name: str) -> uuid.UUID:
    bot_id = uuid.uuid4()
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
            "name": name,
            "color": "#c85a12",
            "icon": "spark",
            "description": "",
            "scenario": "",
            "updated_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        },
    )
    return bot_id


def test_upgrade_gives_an_existing_bot_its_own_secret(database: None) -> None:
    downgrade = _alembic("downgrade", PREVIOUS_REVISION)
    assert downgrade.returncode == 0, downgrade.stderr + downgrade.stdout
    upgrade = None
    try:
        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                assert "inbound_secret" not in _bot_columns(connection)
                account_id = create_account(
                    connection, "inbound-upgrade@example.com", ACCEPTED
                )
                session_id = create_session(connection, account_id)
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == account_id)
                    .values(
                        email_confirmed_at=datetime(
                            2026, 11, 20, tzinfo=timezone.utc
                        )
                    )
                )
                first_id = _insert_old_bot(connection, account_id, "Старый")
                second_id = _insert_old_bot(connection, account_id, "Другой")
        finally:
            engine.dispose()
        upgrade = _alembic("upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout
        first_secret = _stored_secret(str(first_id))
        second_secret = _stored_secret(str(second_id))
        assert first_secret != second_secret
        assert first_secret != str(first_id)
        assert second_secret != str(second_id)
        log = upgrade.stderr + upgrade.stdout
        assert first_secret not in log
        assert second_secret not in log
        assert "/api/hooks/" not in log
        with _client(str(session_id)) as client:
            first = client.get(f"/api/bots/{first_id}")
            second = client.get(f"/api/bots/{second_id}")
            again = client.get(f"/api/bots/{first_id}")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["name"] == "Старый"
        assert second.json()["name"] == "Другой"
        assert first.json()["inbound_webhook"] == inbound_webhook_address(first_secret)
        assert second.json()["inbound_webhook"] == inbound_webhook_address(
            second_secret
        )
        assert _secret_of(first.json()["inbound_webhook"]) == first_secret
        assert _secret_of(second.json()["inbound_webhook"]) == second_secret
        assert again.json()["inbound_webhook"] == first.json()["inbound_webhook"]
    finally:
        restored = _alembic("upgrade", "head")
        assert restored.returncode == 0, restored.stderr + restored.stdout
