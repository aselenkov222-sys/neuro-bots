import json
import logging
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import server.aggregator as aggregator
from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.browser_action import clear_opens
from server.db import create_db_engine
from server.models import (
    accounts,
    quota_charges,
    quota_counters,
    subscription_periods,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO = PROJECT_ROOT.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
EMPTY_TEXT = "Ответ модели не пришёл."
PAGE = "Текст страницы, которую прочитал бот"
ADDRESS = "https://example.com/inbox"
MODEL = "hidden-model"
PASSWORD = "fixture-password-zz"
COOKIE = "fixture-cookie-zz"
KEYCHAIN = "fixture-keychain-zz"
ADAPTER_KEYS = {
    "description",
    "scenario",
    "messages",
    "desktop_available",
    "page_text",
}
JOURNAL_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "server")


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


@pytest.fixture(autouse=True)
def _fresh_opens() -> None:
    clear_opens()
    yield
    clear_opens()


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_ready(email: str) -> tuple[str, uuid.UUID]:
    confirmed_at = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
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


def _quota(account_id: uuid.UUID) -> tuple[dict, list[dict]]:
    periods = [
        row for row in _rows(subscription_periods) if row["account_id"] == account_id
    ]
    counter = [
        row for row in _rows(quota_counters) if row["period_id"] == periods[0]["id"]
    ]
    charges = [
        row for row in _rows(quota_charges) if row["period_id"] == periods[0]["id"]
    ]
    return counter[0], charges


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


def _keys(document: object) -> set[str]:
    found: set[str] = set()
    if isinstance(document, dict):
        found.update(str(key) for key in document)
        for value in document.values():
            found.update(_keys(value))
    elif isinstance(document, list):
        for value in document:
            found.update(_keys(value))
    return found


class _Grab(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _attach_journal() -> tuple[_Grab, list[logging.Logger]]:
    grab = _Grab()
    loggers = [logging.getLogger(name) for name in JOURNAL_LOGGERS]
    for logger in loggers:
        logger.addHandler(grab)
    return grab, loggers


def test_page_reply_keeps_secrets_and_the_model_name_out(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("aggregator-page@example.com")
    computer = uuid.uuid4()
    seen: list[dict] = []
    original = aggregator.complete
    asked = f"открой {ADDRESS}"

    def complete(
        *,
        description: str,
        scenario: str,
        messages: list,
        desktop_available: bool,
        page_text: str,
    ) -> str:
        seen.append(
            {
                "description": description,
                "scenario": scenario,
                "messages": messages,
                "desktop_available": desktop_available,
                "page_text": page_text,
            }
        )
        if len(seen) == 1:
            return ""
        return original(
            description=description,
            scenario=scenario,
            messages=messages,
            desktop_available=desktop_available,
            page_text=page_text,
        )

    monkeypatch.setattr("server.aggregator.complete", complete)
    grab, loggers = _attach_journal()
    try:
        with TestClient(app) as client:
            client.cookies.set("session", cookie)
            claimed = client.post(
                "/api/desktops",
                json={"id": str(computer)},
                headers={"Origin": ORIGIN},
            )
            assert claimed.status_code == 200
            pulsed = client.post(
                f"/api/desktops/{computer}/pulse",
                json={"timezone": "Europe/Moscow"},
                headers={"Origin": ORIGIN},
            )
            assert pulsed.status_code == 200
            created = client.post(
                "/api/bots",
                json={
                    "name": "Бот",
                    "description": "описание",
                    "scenario": "сценарий",
                },
                headers={"Origin": ORIGIN},
            )
            assert created.status_code == 200
            bot_id = created.json()["id"]
            posted = client.post(
                f"/api/bots/{bot_id}/messages",
                json={"text": asked, "model": MODEL},
                headers={"Origin": ORIGIN},
            )
            assert posted.status_code == 200
            assert posted.json()["messages"][-1]["role"] == "user"
            assert "model" not in _keys(posted.json())
            assert MODEL not in posted.text
            task = client.get(f"/api/desktops/{computer}/browser-task")
            assert task.json() == {"url": ADDRESS}
            refused = client.post(
                f"/api/desktops/{computer}/browser-task",
                json={"text": PAGE, "password": PASSWORD, "cookie": COOKIE},
                headers={"Origin": ORIGIN},
            )
            assert refused.status_code == 400
            assert seen == []
            assert PASSWORD not in refused.text
            assert COOKIE not in refused.text
            assert client.get(f"/api/desktops/{computer}/browser-task").json() == {
                "url": ADDRESS
            }
            finished = client.post(
                f"/api/desktops/{computer}/browser-task",
                json={"text": PAGE},
                headers={"Origin": ORIGIN},
            )
            assert finished.status_code == 200
            assert finished.json()["messages"][-1]["role"] == "error"
            assert finished.json()["messages"][-1]["text"] == EMPTY_TEXT
            assert finished.json()["messages"][-1]["code"] == "empty_model_response"
            assert "model" not in _keys(finished.json())
            assert MODEL not in finished.text
            assert seen == [
                {
                    "description": "описание",
                    "scenario": "сценарий",
                    "messages": [{"role": "user", "text": asked}],
                    "desktop_available": True,
                    "page_text": PAGE,
                }
            ]
            assert set(seen[0]) == ADAPTER_KEYS
            assert _keys(seen[0]) == ADAPTER_KEYS | {"role", "text"}
            counter, charges = _quota(account_id)
            assert counter["used"] == 0
            assert charges == []
            follow = client.post(
                f"/api/bots/{bot_id}/messages",
                json={"text": "что дальше"},
                headers={"Origin": ORIGIN},
            )
            assert follow.status_code == 200
            assert follow.json()["messages"][-1]["role"] == "assistant"
            assert follow.json()["messages"][-1]["text"] == STUB
            assert follow.json()["messages"][-1]["code"] is None
            assert "model" not in _keys(follow.json())
            assert MODEL not in follow.text
    finally:
        for logger in loggers:
            logger.removeHandler(grab)
    assert seen[1] == {
        "description": "описание",
        "scenario": "сценарий",
        "messages": [
            {"role": "user", "text": asked},
            {"role": "user", "text": "что дальше"},
        ],
        "desktop_available": True,
        "page_text": "",
    }
    assert set(seen[1]) == ADAPTER_KEYS
    payload = json.dumps(seen, ensure_ascii=False)
    assert PASSWORD not in payload
    assert COOKIE not in payload
    assert KEYCHAIN not in payload
    assert MODEL not in payload
    assert "password" not in _keys(seen)
    assert "cookie" not in _keys(seen)
    journal = "\n".join(grab.lines)
    assert MODEL not in journal
    assert PASSWORD not in journal
    assert COOKIE not in journal
    assert KEYCHAIN not in journal
    stored = _database_text()
    assert PASSWORD not in stored
    assert COOKIE not in stored
    assert KEYCHAIN not in stored
    assert MODEL not in stored
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1


def test_cabinet_sends_text_and_holds_no_model_or_aggregator_key() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        script_response = client.get("/app.js")
        assert script_response.status_code == 200
    script = script_response.text
    assert script == (REPO / "web" / "app.js").read_text(encoding="utf-8")
    assert page.text == (REPO / "web" / "index.html").read_text(encoding="utf-8")
    for piece in ("model", "aggregator_key", "AGGREGATOR_KEY", "apiKey"):
        assert piece not in script
        assert piece not in page.text
    submit = script.split("async function submitMessage() {", 1)[1].split(
        "function restoreFocus(", 1
    )[0]
    assert "body: { text }" in submit
    html = script.split("function messageHtml(message, bot) {", 1)[1].split(
        "function fileChip(", 1
    )[0]
    assert 'esc(message.text || "")' in html
    assert "message.model" not in html
