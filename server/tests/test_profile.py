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
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import accounts, quota_counters, subscription_periods

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
WEB_APP = REPO_ROOT / "web" / "app.js"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
INVALID_NAME = {
    "code": "invalid_name",
    "message": "Имя профиля — до 80 символов.",
}
INVALID_OBJECT = {
    "code": "invalid_request",
    "message": "Нужен JSON-объект.",
}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}


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


def _rows() -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(accounts)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_confirmed(email: str) -> str:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(email_confirmed_at=datetime(2026, 11, 16, tzinfo=timezone.utc))
            )
    finally:
        engine.dispose()
    return str(session_id)


def _client(cookie: str | None = None) -> TestClient:
    client = TestClient(app)
    if cookie is not None:
        client.cookies.set("session", cookie)
    return client


def _patch(client: TestClient, name: object, origin: str | None = ORIGIN):
    headers = {} if origin is None else {"Origin": origin}
    return client.patch("/api/me", json={"name": name}, headers=headers)


def _stored_name(email: str) -> str:
    matched = [row for row in _rows() if row["email"] == email]
    assert len(matched) == 1
    return matched[0]["name"]


def _open_with_counter(email: str, used: int) -> str:
    started = datetime(2026, 11, 16, tzinfo=timezone.utc)
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            connection.execute(
                accounts.update()
                .where(accounts.c.id == account_id)
                .values(email_confirmed_at=started)
            )
            period_id = uuid.uuid4()
            connection.execute(
                subscription_periods.insert().values(
                    id=period_id,
                    account_id=account_id,
                    started_at=started,
                    ends_at=calendar_month_end(started),
                    active=True,
                )
            )
            connection.execute(
                quota_counters.insert().values(
                    id=uuid.uuid4(),
                    period_id=period_id,
                    used=used,
                )
            )
    finally:
        engine.dispose()
    return str(session_id)


def test_profile_name_uses_the_card_field_without_a_prototype_header() -> None:
    script = WEB_APP.read_text(encoding="utf-8")
    start = script.index("function profileScreen()")
    body = script[start : script.index("function catalogForm()")]
    assert 'class="identity raised"' in body
    assert 'class="ink-label" for="profile-name"' in body
    assert 'class="name-input" id="profile-name"' in body
    assert 'maxlength="80"' in body
    assert "proto-nav" not in body
    assert "Кликабельный макет" not in body
    assert '<article class="tariff sheet" aria-label="Тариф"></article>' in body
    assert 'for="quota-counter">Счётчик квоты' in body
    assert "остал" not in body
    assert "500" not in body
    assert "Оплатить" not in body


def test_profile_renames_the_account_including_an_empty_name(database: None) -> None:
    email = f"profile-name-{uuid.uuid4()}@example.com"
    cookie = _open_confirmed(email)
    blank = ""
    spaced = "  Аня  "
    full = "Я" * 80
    with _client(cookie) as client:
        emptied = _patch(client, blank)
        spaced_response = _patch(client, spaced)
        filled = _patch(client, full)
        me = client.get("/api/me")
    assert emptied.status_code == 200
    assert emptied.json()["account"]["name"] == blank
    assert emptied.json()["account"]["email"] == email
    assert spaced_response.status_code == 200
    assert spaced_response.json()["account"]["name"] == spaced
    assert filled.status_code == 200
    assert filled.json()["account"]["name"] == full
    assert me.status_code == 200
    assert me.json()["account"]["name"] == full
    assert _stored_name(email) == full
    assert ACCEPTED not in filled.text


def test_profile_shows_the_period_counter(database: None) -> None:
    email = f"profile-counter-{uuid.uuid4()}@example.com"
    fresh = f"profile-fresh-{uuid.uuid4()}@example.com"
    cookie = _open_with_counter(email, 7)
    with _client(cookie) as client:
        me = client.get("/api/me")
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"email": fresh, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
    assert me.status_code == 200
    assert me.json()["quota_counter"] == 7
    assert me.json()["quota"] == 493
    assert registered.status_code == 200
    assert registered.json()["quota_counter"] is None
    assert "остал" not in me.text


def test_profile_name_rejects_eighty_one_characters_and_a_foreign_origin(
    database: None,
) -> None:
    email = f"profile-limit-{uuid.uuid4()}@example.com"
    other = f"profile-other-{uuid.uuid4()}@example.com"
    cookie = _open_confirmed(email)
    _open_confirmed(other)
    with _client(cookie) as client:
        _patch(client, "Своё")
        too_long = _patch(client, "Я" * 81)
        missing = client.patch("/api/me", json={"name": "Чужое"})
        foreign = _patch(client, "Чужое", origin="http://evil.example")
        raw = client.patch(
            "/api/me",
            content='"Аня"'.encode(),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        extra = client.patch(
            "/api/me",
            json={"name": "Чужое", "email": other},
            headers={"Origin": ORIGIN},
        )
        number = _patch(client, 80)
        me = client.get("/api/me")
    with TestClient(app) as outsider:
        denied = outsider.patch(
            "/api/me",
            json={"name": "Чужое"},
            headers={"Origin": ORIGIN},
        )
    assert too_long.status_code == 400
    assert too_long.json() == INVALID_NAME
    assert missing.status_code == 403
    assert missing.json() == INVALID_ORIGIN
    assert foreign.status_code == 403
    assert foreign.json() == INVALID_ORIGIN
    assert raw.status_code == 400
    assert raw.json() == INVALID_OBJECT
    assert extra.status_code == 400
    assert extra.json() == INVALID_OBJECT
    assert number.status_code == 400
    assert number.json() == INVALID_NAME
    assert denied.status_code == 401
    assert denied.json() == UNAUTHENTICATED
    assert me.json()["account"]["name"] == "Своё"
    assert _stored_name(email) == "Своё"
    assert _stored_name(other) == ""


class Journal:
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


def _wait_until(journal: Journal, predicate, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(journal.text()):
            return
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    assert predicate(journal.text()), journal.text()[-2000:]


def _confirmation_urls(text: str, email: str) -> list[str]:
    suffix = f" {email}"
    found = []
    for line in text.splitlines():
        if "/api/confirm-email?token=" in line and line.endswith(suffix):
            found.append(line[: -len(suffix)])
    return found


def test_profile_screen_saves_the_typed_name(database: None) -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    email = f"profile-screen-{uuid.uuid4()}@example.com"
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
            def ready(text: str) -> bool:
                if "Application startup complete." not in text:
                    return False
                try:
                    return client.get(f"{origin}/health").status_code == 200
                except httpx.HTTPError:
                    return False

            _wait_until(journal, ready)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.goto(f"{origin}/")
            page.fill("#register-email", email)
            page.fill("#register-password", ACCEPTED)
            page.click("button:has-text('Зарегистрироваться')")
            page.wait_for_selector("text=Прислать новую")
            _wait_until(journal, lambda text: len(_confirmation_urls(text, email)) >= 1)
            page.goto(_confirmation_urls(journal.text(), email)[-1])
            page.wait_for_selector("text=Создать бота")
            page.locator("a", has_text="Профиль").click()
            page.wait_for_selector("#profile-name")
            assert page.locator("h1").inner_text() == "Профиль"
            assert "proto-nav" not in page.content()
            tariff = page.locator("article.tariff")
            assert tariff.count() == 1
            assert tariff.inner_text().strip() == ""
            assert page.locator("#quota-counter").input_value() == "0"
            assert page.get_by_text("остал", exact=False).count() == 0
            assert page.get_by_text("500", exact=True).count() == 0
            assert page.get_by_text("Оплатить", exact=True).count() == 0
            with page.expect_response(
                lambda response: response.url.endswith("/api/me")
                and response.request.method == "PATCH"
            ) as saved:
                page.fill("#profile-name", "Аня")
                page.locator("#profile-name").blur()
            assert saved.value.status == 200
            assert page.locator("#profile-name").input_value() == "Аня"
            page.goto(f"{origin}/#profile")
            page.wait_for_selector("#profile-name")
            assert page.locator("#profile-name").input_value() == "Аня"
            with page.expect_response(
                lambda response: response.url.endswith("/api/me")
                and response.request.method == "PATCH"
            ) as cleared:
                page.fill("#profile-name", "")
                page.locator("#profile-name").blur()
            assert cleared.value.status == 200
            page.goto(f"{origin}/#profile")
            page.wait_for_selector("#profile-name")
            assert page.locator("#profile-name").input_value() == ""
            browser.close()
    finally:
        _stop(process, journal)
    assert _stored_name(email) == ""
