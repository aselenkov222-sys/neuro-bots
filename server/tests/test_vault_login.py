import json
import logging
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "desktop" / "src"))

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlalchemy import select, text

import desktop.vault as vault_module
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.sign_in import perform
from desktop.vault import add_record
from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.browser_action import clear_opens
from server.db import create_db_engine
from server.models import accounts, quota_charges, quota_counters, subscription_periods

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
sys.path.insert(0, str(REPO / "desktop" / "src"))

ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
SECRET = "vault-pass-9"
OTHER_SECRET = "vault-pass-8"
COOKIE = "vault-cookie-9"
ADDRESS = "https://example.com/inbox"
SITE = "example.com"
LOGIN = "Аня"
OTHER = "Борис"
PAGE = "страница-после-входа"
ASKED = "страница-выбора"
FORM_PAGE = "страница-входа"
FORM_SENTENCE = f"Сайт {SITE} просит вход. Записи для него нет."
SAVED_FACT = "Доступ сохранён."


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


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _bot(client: TestClient, scenario: str = "сценарий") -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": "описание", "scenario": scenario},
        headers={"Origin": ORIGIN},
    )
    assert created.status_code == 200
    return created.json()


def _claim(client: TestClient, computer: uuid.UUID) -> None:
    claimed = client.post(
        "/api/desktops",
        json={"id": str(computer)},
        headers={"Origin": ORIGIN},
    )
    assert claimed.status_code == 200


def _pulse(client: TestClient, computer: uuid.UUID) -> None:
    pulsed = client.post(
        f"/api/desktops/{computer}/pulse",
        json={"timezone": "Europe/Moscow"},
        headers={"Origin": ORIGIN},
    )
    assert pulsed.status_code == 200


def _used(account_id: uuid.UUID) -> int:
    periods = [
        row
        for row in _rows(subscription_periods)
        if row["account_id"] == account_id
    ]
    counter = [
        row for row in _rows(quota_counters) if row["period_id"] == periods[0]["id"]
    ]
    return counter[0]["used"]


def _rows(table) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


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


def _public_tables() -> list[str]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' ORDER BY tablename"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()


class _Grab(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _journal() -> tuple[_Grab, list[logging.Logger]]:
    grab = _Grab()
    loggers = [
        logging.getLogger(name)
        for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "server")
    ]
    for logger in loggers:
        logger.addHandler(grab)
    return grab, loggers


def _forbid_real_security(*args, **kwargs):
    raise AssertionError("real security was called")


class FakeSecurity:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.items: dict[tuple[str, str], str] = {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
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


class FakePage:
    def __init__(self, before: str, after: str) -> None:
        self.before = before
        self.after = after
        self.filled: list[str] = []
        self.submitted = False

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self, selector)

    def inner_text(self, selector: str) -> str:
        return self.after if self.submitted else self.before

    def close(self) -> None:
        return None


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        if "password" in self.selector:
            return 1
        return 1

    def fill(self, value: str) -> None:
        self.page.filled.append(value)

    def click(self) -> None:
        self.page.submitted = True

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.page, selector)


def _finds(security: FakeSecurity) -> int:
    return sum(command[1] == "find-generic-password" for command in security.commands)


@pytest.fixture
def security(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSecurity:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    return FakeSecurity()


def test_named_record_is_filled_on_the_desktop_and_stays_off_the_server(
    database: None, security: FakeSecurity
) -> None:
    cookie, account_id = _open_ready(f"named-{uuid.uuid4()}@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client, "открой почту Аня")
        add_record(SITE, LOGIN, SECRET, shared=False, bot_id=bot["id"], run=security)
        page = FakePage(PAGE, f"{PAGE} {SECRET}")
        result = perform(
            ADDRESS, bot["id"], "открой почту Аня", run=security, page=page
        )
        encoded = json.dumps(result, ensure_ascii=False)
        assert result["login"] == "entered"
        assert result["text"] == f"{PAGE} "
        assert SECRET not in encoded
        assert page.filled[-1] == SECRET
        assert _finds(security) == 1
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": f"открой {ADDRESS}"},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        assert posted.json()["messages"][-1]["role"] == "user"
        assert SECRET not in posted.request.content.decode()
        refused = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": PAGE, "login": "entered", "password": SECRET, "cookie": COOKIE},
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert SECRET not in refused.text
        assert COOKIE not in refused.text
        assert client.get(f"/api/desktops/{computer}/browser-task").json() == {
            "url": ADDRESS
        }
        report = {"text": result["text"], "login": "entered"}
        assert SECRET not in json.dumps(report, ensure_ascii=False)
        grab, loggers = _journal()
        try:
            finished = client.post(
                f"/api/desktops/{computer}/browser-task",
                json=report,
                headers={"Origin": ORIGIN},
            )
        finally:
            for logger in loggers:
                logger.removeHandler(grab)
        assert finished.status_code == 200
        assert SECRET not in finished.request.content.decode()
        assert finished.json()["messages"][-1]["text"] == STUB
        journal = "\n".join(grab.lines)
    assert SECRET not in journal
    assert COOKIE not in journal
    assert PAGE not in journal
    stored = _database_text()
    assert SECRET not in stored
    assert COOKIE not in stored
    assert PAGE not in stored
    assert STUB in stored
    tables = _public_tables()
    assert tables
    assert all("vault" not in name for name in tables)
    models = (REPO / "server" / "src" / "server" / "models.py").read_text(encoding="utf-8")
    assert "vault" not in models
    assert _used(account_id) == 1
    assert len(_rows(quota_charges)) >= 1


def test_a_choice_asks_without_the_keychain_and_the_reply_fills(
    database: None, security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []

    def complete(**kwargs) -> str:
        seen.append(kwargs)
        return STUB

    monkeypatch.setattr("server.aggregator.complete", complete)
    cookie, account_id = _open_ready(f"choice-{uuid.uuid4()}@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client, f"{LOGIN} или {OTHER}")
        add_record(SITE, LOGIN, SECRET, shared=True, run=security)
        add_record(SITE, OTHER, OTHER_SECRET, shared=False, bot_id=bot["id"], run=security)
        page = FakePage(ASKED, f"{ASKED} {SECRET}")
        asked = perform(ADDRESS, bot["id"], f"{LOGIN} или {OTHER}", run=security, page=page)
        assert asked["login"] == "choose"
        assert asked["choices"] == [LOGIN, OTHER]
        assert _finds(security) == 0
        assert page.submitted is False
        opened = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": f"открой {ADDRESS}"},
            headers={"Origin": ORIGIN},
        )
        assert opened.json()["messages"][-1]["role"] == "user"
        question = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={
                "text": asked["text"],
                "login": "choose",
                "site": asked["site"],
                "choices": asked["choices"],
            },
            headers={"Origin": ORIGIN},
        )
        assert question.status_code == 200
        assert question.json()["messages"][-1]["text"] == (
            f"Какую запись использовать для {SITE}? {LOGIN} или {OTHER}."
        )
        assert SECRET not in question.text
        assert seen == []
        assert _used(account_id) == 0
        assert client.get(f"/api/desktops/{computer}/browser-task").status_code == 204
        ordinary = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "привет"},
            headers={"Origin": ORIGIN},
        )
        assert ordinary.json()["messages"][-1]["text"] == STUB
        assert client.get(f"/api/desktops/{computer}/browser-task").status_code == 204
        assert SECRET not in json.dumps(seen, ensure_ascii=False)
        chosen = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": LOGIN},
            headers={"Origin": ORIGIN},
        )
        assert chosen.json()["messages"][-1]["role"] == "user"
        task = client.get(f"/api/desktops/{computer}/browser-task")
        assert task.json() == {"url": ADDRESS, "login": LOGIN}
        assert "password" not in task.json()
        filled = perform(
            ADDRESS,
            bot["id"],
            f"{LOGIN} или {OTHER}",
            chosen=LOGIN,
            run=security,
            page=page,
        )
        assert filled["login"] == "entered"
        assert SECRET not in json.dumps(filled, ensure_ascii=False)
        assert page.filled[-1] == SECRET
        assert _finds(security) == 1
        report = {"text": filled["text"], "login": "entered"}
        assert SECRET not in json.dumps(report, ensure_ascii=False)
        grab, loggers = _journal()
        try:
            finished = client.post(
                f"/api/desktops/{computer}/browser-task",
                json=report,
                headers={"Origin": ORIGIN},
            )
        finally:
            for logger in loggers:
                logger.removeHandler(grab)
        assert finished.status_code == 200
        assert SECRET not in finished.request.content.decode()
        journal = "\n".join(grab.lines)
    assert SECRET not in journal
    assert OTHER_SECRET not in journal
    stored = _database_text()
    assert SECRET not in stored
    assert OTHER_SECRET not in stored
    assert ASKED not in stored
    assert _used(account_id) == 2


def test_missing_record_saves_the_fact_without_the_password(
    database: None, security: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []

    def complete(**kwargs) -> str:
        seen.append(kwargs)
        return STUB

    monkeypatch.setattr("server.aggregator.complete", complete)
    cookie, account_id = _open_ready(f"form-{uuid.uuid4()}@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client, f"почта {LOGIN}")
        add_record(SITE, LOGIN, SECRET, shared=False, bot_id="bot-boris", run=security)
        page = FakePage(FORM_PAGE, FORM_PAGE)
        missing = perform(ADDRESS, bot["id"], f"почта {LOGIN}", run=security, page=page)
        assert missing == {"text": FORM_PAGE, "login": "form", "site": SITE}
        assert _finds(security) == 0
        assert page.filled == []
        assert page.submitted is False
        client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": f"открой {ADDRESS}"},
            headers={"Origin": ORIGIN},
        )
        formed = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": missing["text"], "login": "form", "site": missing["site"]},
            headers={"Origin": ORIGIN},
        )
        assert formed.status_code == 200
        assert formed.json()["messages"][-1]["text"] == FORM_SENTENCE
        assert SECRET not in formed.text
        saved = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"login": "saved", "site": SITE},
            headers={"Origin": ORIGIN},
        )
        assert saved.status_code == 200
        assert saved.json()["messages"][-1]["text"] == SAVED_FACT
        assert SECRET not in saved.request.content.decode()
        assert seen == []
        assert _used(account_id) == 0
        later = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "что нового"},
            headers={"Origin": ORIGIN},
        )
        assert later.status_code == 200
        payload = json.dumps(seen, ensure_ascii=False)
        assert SAVED_FACT in payload
        assert FORM_SENTENCE in payload
        assert SECRET not in payload
        assert FORM_PAGE not in payload
    stored = _database_text()
    assert SECRET not in stored
    assert FORM_PAGE not in stored
    assert SAVED_FACT in stored
    assert _used(account_id) == 1


def test_session_login_behaves_like_page_text(database: None) -> None:
    cookie, account_id = _open_ready(f"session-{uuid.uuid4()}@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client)
        client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": f"открой {ADDRESS}"},
            headers={"Origin": ORIGIN},
        )
        finished = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": PAGE, "login": "session"},
            headers={"Origin": ORIGIN},
        )
        assert finished.status_code == 200
        assert finished.json()["messages"][-1]["text"] == STUB
    assert PAGE not in _database_text()
    assert _used(account_id) == 1


def test_cabinet_shows_the_access_link(database: None) -> None:
    port = _free_port()
    origin = f"http://{LOOPBACK}:{port}"
    cookie, _account_id = _open_ready(f"link-{uuid.uuid4()}@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client)
        client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": f"открой {ADDRESS}"},
            headers={"Origin": ORIGIN},
        )
        formed = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": FORM_PAGE, "login": "form", "site": SITE},
            headers={"Origin": ORIGIN},
        )
        assert formed.status_code == 200
        bot_id = bot["id"]
    screen = serve_password_screen()
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
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=5) as probe:
            while time.monotonic() < deadline:
                try:
                    if probe.get(f"{origin}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            assert probe.get(f"{origin}/health").status_code == 200
        bridge = (
            "window.__neuroDesktop = "
            + json.dumps(
                {
                    "id": str(computer),
                    "timezone": "Europe/Moscow",
                    "openUrl": screen.url,
                    "openToken": screen.open_token,
                }
            )
            + ";"
        )
        with sync_playwright() as playwright:
            chrome = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = chrome.new_context()
            context.add_cookies(
                [{"name": "session", "value": cookie, "url": origin}]
            )
            context.add_init_script(bridge)
            page = context.new_page()
            page.goto(f"{origin}/#bots")
            page.wait_for_selector(".bot-card")
            page.click(".bot-card")
            page.wait_for_selector("text=В чат")
            page.click("text=В чат")
            page.wait_for_selector("text=Ввести доступ", timeout=20000)
            link = page.get_by_role("link", name="Ввести доступ")
            href = link.get_attribute("href") or ""
            assert f"bot={bot_id}" in href
            assert f"site={SITE}" in href
            assert SECRET not in href
            assert "password" not in href
            assert COOKIE not in href
            with page.expect_popup() as popup:
                link.click()
            form = popup.value
            form.wait_for_selector("text=Защищённая форма")
            content = form.content()
            assert SECRET not in content
            assert "http:" not in content
            assert "https:" not in content
            assert 'name="request_password"' in content
            assert "Сделать запись общей" in content
            chrome.close()
        stored = _database_text()
        assert SECRET not in stored
        assert FORM_PAGE not in stored
        assert FORM_SENTENCE in stored
    finally:
        screen.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]
