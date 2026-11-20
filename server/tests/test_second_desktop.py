import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.aggregator import CompleteResult
from server.app import app
from server.db import create_db_engine
from server.models import accounts, quota_counters, subscription_periods

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
REPLY = "Таблица готова."
XLSX_NAME = "письма.xlsx"
XLSX_BYTES = b"PK\x03\x04\x00second-desktop-xlsx"
SCENARIO = "Открыть почту и собрать таблицу."


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


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_account(email: str) -> tuple[str, uuid.UUID]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            confirmed_at = datetime(2026, 11, 16, tzinfo=timezone.utc)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(email_confirmed_at=confirmed_at)
            )
            period_id = uuid.uuid4()
            connection.execute(
                subscription_periods.insert().values(
                    id=period_id,
                    account_id=account_id,
                    started_at=confirmed_at,
                    ends_at=calendar_month_end(confirmed_at),
                    active=True,
                )
            )
            connection.execute(
                quota_counters.insert().values(
                    id=uuid.uuid4(),
                    period_id=period_id,
                    used=0,
                )
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _login(email: str) -> str:
    with TestClient(app) as fresh:
        logged = fresh.post(
            "/api/login",
            json={"email": email, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
    assert logged.status_code == 200
    cookie = fresh.cookies.get("session")
    assert isinstance(cookie, str)
    return cookie


def _post_desktop(client: TestClient, computer: uuid.UUID):
    return client.post(
        "/api/desktops",
        json={"id": str(computer)},
        headers={"Origin": ORIGIN},
    )


def test_second_desktop_reads_bots_chats_scenarios_routines_and_library(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"second-read-{uuid.uuid4()}@example.com"
    cookie, _account_id = _open_account(email)

    def complete(**kwargs: object) -> CompleteResult:
        return CompleteResult(REPLY, (XLSX_NAME, XLSX_BYTES))

    monkeypatch.setattr("server.aggregator.complete", complete)
    first = uuid.uuid4()
    with _client(cookie) as client:
        created = client.post(
            "/api/bots",
            json={
                "name": "Почта",
                "description": "Разбирает письма",
                "scenario": SCENARIO,
            },
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        bot = created.json()
        routine = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [1, 3, 5], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        assert routine.status_code == 200
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "собери таблицу"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        extra = client.post(
            f"/api/bots/{bot['id']}/chats",
            headers={"Origin": ORIGIN},
        )
        assert extra.status_code == 200
        extra_id = extra.json()["id"]
        extra_message = client.post(
            f"/api/bots/{bot['id']}/chats/{extra_id}/messages",
            json={"text": "ещё одна таблица"},
            headers={"Origin": ORIGIN},
        )
        assert extra_message.status_code == 200
        claimed = _post_desktop(client, first)
        assert claimed.json() == {"id": str(first), "working": True}
    second_cookie = _login(email)
    assert second_cookie != cookie
    other = uuid.uuid4()
    with _client(second_cookie) as client:
        signed = _post_desktop(client, other)
        assert signed.status_code == 200
        assert signed.json() == {"id": str(other), "working": False}
        bots = client.get("/api/bots")
        card = client.get(f"/api/bots/{bot['id']}")
        chats = client.get(f"/api/bots/{bot['id']}/chats")
        main_id = next(
            item["id"] for item in chats.json()["chats"] if item["kind"] == "main"
        )
        main_messages = client.get(f"/api/bots/{bot['id']}/messages")
        extra_messages = client.get(
            f"/api/bots/{bot['id']}/chats/{extra_id}/messages"
        )
        routines = client.get(f"/api/bots/{bot['id']}/routines")
        library = client.get(f"/api/bots/{bot['id']}/library")
        file_id = library.json()["files"][0]["id"]
        downloaded = client.get(f"/api/bots/{bot['id']}/library/{file_id}")
    assert bots.status_code == 200
    assert [item["id"] for item in bots.json()["bots"]] == [bot["id"]]
    assert card.status_code == 200
    assert card.json()["name"] == "Почта"
    assert card.json()["description"] == "Разбирает письма"
    assert card.json()["scenario"] == SCENARIO
    assert chats.status_code == 200
    assert {item["id"] for item in chats.json()["chats"]} == {main_id, extra_id}
    assert main_messages.status_code == 200
    assert [item["text"] for item in main_messages.json()["messages"]] == [
        "собери таблицу",
        REPLY,
    ]
    assert main_messages.json()["messages"][1]["file"]["name"] == XLSX_NAME
    assert extra_messages.status_code == 200
    assert [item["text"] for item in extra_messages.json()["messages"]] == [
        "ещё одна таблица",
        REPLY,
    ]
    assert routines.status_code == 200
    assert routines.json() == [routine.json()]
    assert library.status_code == 200
    assert [item["name"] for item in library.json()["files"]] == [XLSX_NAME, XLSX_NAME]
    assert downloaded.status_code == 200
    assert downloaded.content == XLSX_BYTES
    assert XLSX_BYTES not in bots.content
    assert "password" not in card.json()
    assert "cookie" not in card.json()
