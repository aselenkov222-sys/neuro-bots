import json
import logging
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
from sqlalchemy import select, text

from server.accounts import calendar_month_end, create_account, create_session
from server.app import app
from server.browser_action import clear_opens
from server.db import create_db_engine
from server.models import (
    accounts,
    chats,
    messages,
    quota_charges,
    quota_counters,
    subscription_periods,
)

REPO = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
sys.path.insert(0, str(REPO / "desktop" / "src"))

from desktop.browser import open_address  # noqa: E402
from desktop.password_screen import LOOPBACK, serve_password_screen  # noqa: E402

ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
EMPTY_TEXT = "Ответ модели не пришёл."
COMPUTER_UNAVAILABLE = "компьютер недоступен"
PAGE = "Текст страницы"
SECRET = "vault-pass-9"
COOKIE = "vault-cookie-9"
ADDRESS = "https://example.com/inbox"


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


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _bot(client: TestClient) -> dict:
    created = client.post(
        "/api/bots",
        json={"name": "Бот", "description": "описание", "scenario": "сценарий"},
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
    assert claimed.json()["working"] is True


def _pulse(client: TestClient, computer: uuid.UUID) -> None:
    pulsed = client.post(
        f"/api/desktops/{computer}/pulse",
        json={"timezone": "Europe/Moscow"},
        headers={"Origin": ORIGIN},
    )
    assert pulsed.status_code == 200


def _post_message(client: TestClient, bot_id: str, text: str):
    return client.post(
        f"/api/bots/{bot_id}/messages",
        json={"text": text},
        headers={"Origin": ORIGIN},
    )


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


class _Grab(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _opened_page(url: str) -> str:
    calls: list[tuple[object, ...]] = []

    class _Page:
        def goto(self, address: str, *args: object, **kwargs: object) -> None:
            calls.append(("goto", address, args, kwargs))

        def inner_text(self, selector: str) -> str:
            calls.append(("inner_text", selector))
            return PAGE

        def click(self, *args: object, **kwargs: object) -> None:
            calls.append(("click",))

        def fill(self, *args: object, **kwargs: object) -> None:
            calls.append(("fill",))

        def type(self, *args: object, **kwargs: object) -> None:
            calls.append(("type",))

        def close(self) -> None:
            calls.append(("close",))

    class _Context:
        def new_page(self) -> _Page:
            return _Page()

        def cookies(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
            calls.append(("cookies", COOKIE))
            return [{"name": "session", "value": COOKIE}]

    import desktop.browser as browser

    held = list(browser._held)
    browser._held[:] = [(None, _Context())]
    try:
        text = open_address(url)
    finally:
        browser._held[:] = held
    assert calls == [
        ("goto", url, (), {}),
        ("inner_text", "body"),
        ("close",),
    ]
    assert COOKIE not in str(calls)
    return text


def test_named_address_opens_and_the_stub_lands_in_the_same_chat(
    database: None,
) -> None:
    cookie, account_id = _open_ready("open-address@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client)
        posted = _post_message(client, bot["id"], f"открой {ADDRESS}.")
        assert posted.status_code == 200
        assert [message["role"] for message in posted.json()["messages"]] == ["user"]
        assert posted.json()["messages"][0]["text"] == f"открой {ADDRESS}."
        task = client.get(f"/api/desktops/{computer}/browser-task")
        assert task.status_code == 200
        assert task.json() == {"url": ADDRESS}
        assert _quota(account_id)[0]["used"] == 0
        page = _opened_page(task.json()["url"])
        finished = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": page},
            headers={"Origin": ORIGIN},
        )
        assert finished.status_code == 200
        roles = [message["role"] for message in finished.json()["messages"]]
        assert roles == ["user", "assistant"]
        assert finished.json()["messages"][1]["text"] == STUB
        assert finished.json()["messages"][1]["code"] is None
        me = client.get("/api/me")
        idle = client.get(f"/api/desktops/{computer}/browser-task")
    assert idle.status_code == 204
    assert me.json()["quota"] == 499
    counter, charges = _quota(account_id)
    assert counter["used"] == 1
    assert len(charges) == 1
    stored = _database_text()
    assert STUB in stored
    assert PAGE not in stored
    assert SECRET not in stored
    assert COOKIE not in stored


def test_without_a_pulse_the_browser_stays_closed(database: None) -> None:
    cookie, account_id = _open_ready("silent-address@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        bot = _bot(client)
        silent = _post_message(client, bot["id"], f"открой {ADDRESS}")
        assert silent.status_code == 200
        assert [message["role"] for message in silent.json()["messages"]] == [
            "user",
            "error",
        ]
        assert silent.json()["messages"][1]["text"] == COMPUTER_UNAVAILABLE
        task = client.get(f"/api/desktops/{computer}/browser-task")
        assert task.status_code == 204
        absent_cookie, _absent = _open_ready("no-desktop-address@example.com")
    with _client(absent_cookie) as client:
        bot = _bot(client)
        absent = _post_message(client, bot["id"], f"открой {ADDRESS}")
        assert absent.status_code == 200
        assert absent.json()["messages"][1]["text"] == COMPUTER_UNAVAILABLE
        missing = client.get(f"/api/desktops/{uuid.uuid4()}/browser-task")
        assert missing.status_code == 404
    assert _quota(account_id)[0]["used"] == 0
    assert PAGE not in _database_text()


def test_page_text_is_not_stored_and_errors_stay_out_of_the_adapter(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie, account_id = _open_ready("page-text@example.com")
    computer = uuid.uuid4()
    seen: list[dict] = []

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
        return "ответ"

    monkeypatch.setattr("server.aggregator.complete", complete)
    grab = _Grab()
    loggers = [
        logging.getLogger(name)
        for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "server")
    ]
    for logger in loggers:
        logger.addHandler(grab)
    try:
        with _client(cookie) as client:
            _claim(client, computer)
            bot = _bot(client)
            blocked = _post_message(client, bot["id"], f"сначала {ADDRESS}")
            assert blocked.json()["messages"][-1]["text"] == COMPUTER_UNAVAILABLE
            _pulse(client, computer)
            posted = _post_message(client, bot["id"], f"теперь {ADDRESS}")
            assert posted.json()["messages"][-1]["role"] == "user"
            task = client.get(f"/api/desktops/{computer}/browser-task")
            assert task.json() == {"url": ADDRESS}
            finished = client.post(
                f"/api/desktops/{computer}/browser-task",
                json={"text": PAGE},
                headers={"Origin": ORIGIN},
            )
            assert finished.status_code == 200
            assert finished.json()["messages"][-1] == {
                **finished.json()["messages"][-1],
                "role": "assistant",
                "text": "ответ",
                "code": None,
            }
            assert seen == [
                {
                    "description": "описание",
                    "scenario": "сценарий",
                    "messages": [
                        {"role": "user", "text": f"сначала {ADDRESS}"},
                        {"role": "user", "text": f"теперь {ADDRESS}"},
                    ],
                    "desktop_available": True,
                    "page_text": PAGE,
                }
            ]
            assert seen[0]["page_text"] == PAGE
            assert _quota(account_id)[0]["used"] == 1
            follow = _post_message(client, bot["id"], "что дальше")
            assert follow.status_code == 200
            assert follow.json()["messages"][-1]["role"] == "assistant"
            assert follow.json()["messages"][-1]["text"] == "ответ"
    finally:
        for logger in loggers:
            logger.removeHandler(grab)
    assert seen[1] == {
        "description": "описание",
        "scenario": "сценарий",
        "messages": [
            {"role": "user", "text": f"сначала {ADDRESS}"},
            {"role": "user", "text": f"теперь {ADDRESS}"},
            {"role": "assistant", "text": "ответ"},
            {"role": "user", "text": "что дальше"},
        ],
        "desktop_available": True,
        "page_text": "",
    }
    assert COMPUTER_UNAVAILABLE not in str(seen)
    assert "error" not in str(seen)
    stored = _database_text()
    assert PAGE not in stored
    journal = "\n".join(grab.lines)
    assert PAGE not in journal
    chat_id = [row for row in _rows(chats) if row["bot_id"] == uuid.UUID(bot["id"])][0]["id"]
    roles = [
        row["role"]
        for row in _rows(messages)
        if row["chat_id"] == chat_id
    ]
    assert roles == ["user", "error", "user", "assistant", "user", "assistant"]


def test_empty_stub_does_not_charge(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    cookie, account_id = _open_ready("empty-open@example.com")
    computer = uuid.uuid4()
    monkeypatch.setattr("server.aggregator.complete", lambda **_kwargs: "")
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        bot = _bot(client)
        posted = _post_message(client, bot["id"], f"открой {ADDRESS}")
        assert [message["role"] for message in posted.json()["messages"]] == ["user"]
        finished = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": PAGE},
            headers={"Origin": ORIGIN},
        )
        assert finished.status_code == 200
        assert finished.json()["messages"][-1]["role"] == "error"
        assert finished.json()["messages"][-1]["text"] == EMPTY_TEXT
        assert finished.json()["messages"][-1]["code"] == "empty_model_response"
        me = client.get("/api/me")
    assert me.json()["quota"] == 500
    counter, charges = _quota(account_id)
    assert counter["used"] == 0
    assert charges == []
    assert PAGE not in _database_text()


def test_password_and_cookies_stay_off_the_server(database: None) -> None:
    cookie, _account_id = _open_ready("secrets-open@example.com")
    computer = uuid.uuid4()
    grab = _Grab()
    loggers = [
        logging.getLogger(name)
        for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "server")
    ]
    for logger in loggers:
        logger.addHandler(grab)
    try:
        with _client(cookie) as client:
            _claim(client, computer)
            _pulse(client, computer)
            bot = _bot(client)
            posted = _post_message(client, bot["id"], f"открой {ADDRESS}")
            assert json.loads(posted.request.content) == {"text": f"открой {ADDRESS}"}
            refused = client.post(
                f"/api/desktops/{computer}/browser-task",
                json={"text": PAGE, "password": SECRET, "cookie": COOKIE, "login": "Аня"},
                headers={"Origin": ORIGIN},
            )
            assert refused.status_code == 400
            assert SECRET not in refused.text
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
            assert finished.json()["messages"][-1]["text"] == STUB
    finally:
        for logger in loggers:
            logger.removeHandler(grab)
    journal = "\n".join(grab.lines)
    assert SECRET not in journal
    assert COOKIE not in journal
    assert PAGE not in journal
    stored = _database_text()
    assert SECRET not in stored
    assert COOKIE not in stored
    assert PAGE not in stored
    assert STUB in stored


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


def test_cabinet_opens_the_named_address(database: None) -> None:
    port = _free_port()
    origin = f"http://{LOOPBACK}:{port}"
    email = f"cabinet-open-{uuid.uuid4()}@example.com"
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
    screen = serve_password_screen()
    screen.httpd.cabinet_origin = origin
    opened: list[str] = []

    def fake_open(url: str) -> str:
        opened.append(url)
        return PAGE

    import desktop.browser as browser

    original = browser.open_address
    browser.open_address = fake_open
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=5) as client:
            while time.monotonic() < deadline:
                if "Application startup complete." in journal.text():
                    try:
                        if client.get(f"{origin}/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(0.05)
            assert "Application startup complete." in journal.text()
            registered = client.post(
                f"{origin}/api/register",
                json={"email": email, "password": ACCEPTED},
                headers={"Origin": origin},
            )
            assert registered.status_code == 200
            link_deadline = time.monotonic() + 10
            link = ""
            while time.monotonic() < link_deadline and not link:
                for line in journal.text().splitlines():
                    if email in line and "/api/confirm-email?token=" in line:
                        link = line.split()[0]
                time.sleep(0.05)
            assert link
            confirmed = client.get(link, headers={"Accept": "application/json"})
            assert confirmed.status_code == 200
            assert confirmed.json()["destination"] == "bots"
            created = client.post(
                f"{origin}/api/bots",
                json={"name": "Бот", "description": "описание", "scenario": "сценарий"},
                headers={"Origin": origin},
            )
            assert created.status_code == 200
            session = client.cookies.get("session")
            assert session
            computer = str(uuid.uuid4())
            bridge = (
                "window.__neuroDesktop = "
                + json.dumps(
                    {
                        "id": computer,
                        "timezone": "Europe/Moscow",
                        "openUrl": screen.url,
                        "openToken": screen.open_token,
                    }
                )
                + ";"
            )
            posts: list[tuple[str, str]] = []
            with sync_playwright() as playwright:
                chrome = playwright.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = chrome.new_context()
                context.add_cookies(
                    [
                        {
                            "name": "session",
                            "value": session,
                            "url": origin,
                        }
                    ]
                )
                context.add_init_script(bridge)
                page = context.new_page()

                def remember(request) -> None:
                    if request.method == "POST" and request.post_data:
                        posts.append((request.url, request.post_data))

                page.on("request", remember)
                with page.expect_response("**/api/desktops/*/pulse"):
                    page.goto(f"{origin}/#bots")
                page.wait_for_selector(".bot-card")
                page.click(".bot-card")
                page.wait_for_selector("text=В чат")
                page.click("text=В чат")
                page.wait_for_selector("#composer")
                page.fill("#composer", f"открой {ADDRESS}")
                page.click("button[aria-label='Отправить']")
                page.wait_for_selector(f"text={STUB}", timeout=20000)
                assert page.locator(".bubble").filter(has_text=STUB).count() == 1
                chrome.close()
        assert opened == [ADDRESS]
        assert any(
            json.loads(body).get("url") == ADDRESS
            for url, body in posts
            if url.endswith("/sign-in") or url.endswith("/open")
        )
        assert not any(SECRET in body or COOKIE in body for _url, body in posts)
        assert all(
            set(json.loads(body)) == {"text"}
            for url, body in posts
            if url.endswith("/messages") or "browser-task" in url
        )
        me = httpx.get(f"{origin}/api/me", cookies={"session": session})
        assert me.json()["quota"] == 499
        time.sleep(0.2)
        logged = journal.text()
        assert PAGE not in logged
        assert SECRET not in logged
        assert COOKIE not in logged
        stored = _database_text()
        assert PAGE not in stored
        assert SECRET not in stored
        assert COOKIE not in stored
        assert STUB in stored
    finally:
        browser.open_address = original
        screen.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        journal.join()


def test_creating_or_renaming_a_bot_sends_its_name(database: None) -> None:
    port = _free_port()
    origin = f"http://{LOOPBACK}:{port}"
    email = f"cabinet-names-{uuid.uuid4()}@example.com"
    kept = "Склад"
    created_name = "Маяк"
    renamed = "Буй"
    note = "заметка-карточки"
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
    screen = serve_password_screen()
    screen.httpd.cabinet_origin = origin
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=5) as client:
            while time.monotonic() < deadline:
                if "Application startup complete." in journal.text():
                    try:
                        if client.get(f"{origin}/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(0.05)
            assert "Application startup complete." in journal.text()
            registered = client.post(
                f"{origin}/api/register",
                json={"email": email, "password": ACCEPTED},
                headers={"Origin": origin},
            )
            assert registered.status_code == 200
            link_deadline = time.monotonic() + 10
            link = ""
            while time.monotonic() < link_deadline and not link:
                for line in journal.text().splitlines():
                    if email in line and "/api/confirm-email?token=" in line:
                        link = line.split()[0]
                time.sleep(0.05)
            assert link
            confirmed = client.get(link, headers={"Accept": "application/json"})
            assert confirmed.status_code == 200
            existing = client.post(
                f"{origin}/api/bots",
                json={"name": kept, "description": "старое описание", "scenario": "сценарий"},
                headers={"Origin": origin},
            )
            assert existing.status_code == 200
            kept_id = existing.json()["id"]
            session = client.cookies.get("session")
            assert session
            computer = str(uuid.uuid4())
            bridge = (
                "window.__neuroDesktop = "
                + json.dumps(
                    {
                        "id": computer,
                        "timezone": "Europe/Moscow",
                        "openUrl": screen.url,
                        "openToken": screen.open_token,
                    }
                )
                + ";"
            )
            list_loads: list[str] = []
            with sync_playwright() as playwright:
                chrome = playwright.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = chrome.new_context()
                context.add_cookies(
                    [
                        {
                            "name": "session",
                            "value": session,
                            "url": origin,
                        }
                    ]
                )
                context.add_init_script(bridge)
                page = context.new_page()

                def remember_list(request) -> None:
                    path = request.url.split("?", 1)[0].rstrip("/")
                    if request.method == "GET" and path.endswith("/api/bots"):
                        list_loads.append(request.url)

                page.on("request", remember_list)

                def bot_names(request) -> bool:
                    return request.method == "POST" and request.url.endswith("/bot-names")

                with page.expect_response("**/api/desktops/*/pulse"):
                    with page.expect_request(bot_names):
                        page.goto(f"{origin}/#bots")
                page.wait_for_selector(".bot-card")
                page.get_by_role("button", name="Создать бота").click()
                page.wait_for_selector("#create-name")
                page.fill("#create-name", created_name)
                page.fill("#create-desc", note)
                page.locator("#save-bot:not([disabled])").wait_for()
                with page.expect_request(bot_names) as created_post:
                    page.click("#save-bot")
                page.locator(".bot-card").filter(has_text=created_name).wait_for()
                assert page.url.endswith("#bots")
                created_body = created_post.value.post_data or ""
                created_bots = _bot_name_posts(created_body)
                assert {bot["name"] for bot in created_bots} == {kept, created_name}
                assert any(bot["id"] == kept_id for bot in created_bots)
                assert note not in created_body
                assert "старое описание" not in created_body
                assert "сценарий" not in created_body
                page.locator(".bot-card").filter(has_text=created_name).click()
                page.wait_for_selector("#bot-name")
                assert page.url.endswith("#bot")
                with page.expect_request(bot_names) as renamed_post:
                    page.fill("#bot-name", renamed)
                    page.locator("#bot-name").blur()
                page.wait_for_selector(f"text={renamed}")
                assert page.url.endswith("#bot")
                renamed_body = renamed_post.value.post_data or ""
                renamed_bots = _bot_name_posts(renamed_body)
                assert {bot["name"] for bot in renamed_bots} == {kept, renamed}
                assert any(bot["id"] == kept_id for bot in renamed_bots)
                assert created_name not in renamed_body
                assert note not in renamed_body
                chrome.close()
        assert list_loads == [f"{origin}/api/bots"]
    finally:
        screen.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        journal.join()


def _bot_name_posts(body: str) -> list[dict[str, str]]:
    assert ACCEPTED not in body
    assert "password" not in body
    payload = json.loads(body)
    assert set(payload) == {"bots"}
    bots = payload["bots"]
    assert isinstance(bots, list)
    assert all(set(bot) == {"id", "name"} for bot in bots)
    return bots


def test_a_message_to_the_busy_bot_waits_and_another_answers(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def spy(**kwargs: object) -> str:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        calls.append([item["text"] for item in messages])
        return STUB

    monkeypatch.setattr("server.aggregator.complete", spy)
    cookie, account_id = _open_ready("busy-bot@example.com")
    computer = uuid.uuid4()
    with _client(cookie) as client:
        _claim(client, computer)
        _pulse(client, computer)
        busy = _bot(client)
        other = client.post(
            "/api/bots",
            json={
                "name": "Другой",
                "description": "описание",
                "scenario": "сценарий",
            },
            headers={"Origin": ORIGIN},
        )
        assert other.status_code == 200
        opened = _post_message(client, busy["id"], f"открой {ADDRESS}")
        assert [item["role"] for item in opened.json()["messages"]] == ["user"]
        assert calls == []
        held = _post_message(client, busy["id"], "просто текст")
        assert [item["role"] for item in held.json()["messages"]] == ["user", "user"]
        assert calls == []
        immediate = _post_message(client, other.json()["id"], "привет")
        assert immediate.status_code == 200
        assert immediate.json()["messages"][-1]["role"] == "assistant"
        assert immediate.json()["messages"][-1]["text"] == STUB
        assert calls == [["привет"]]
        finished = client.post(
            f"/api/desktops/{computer}/browser-task",
            json={"text": PAGE},
            headers={"Origin": ORIGIN},
        )
        assert finished.status_code == 200
        assert [item["role"] for item in finished.json()["messages"]] == [
            "user",
            "user",
            "assistant",
            "assistant",
        ]
        assert finished.json()["messages"][2]["text"] == STUB
        assert finished.json()["messages"][3]["text"] == STUB
    assert len(calls) == 3
    assert calls[1][0] == f"открой {ADDRESS}"
    assert "просто текст" in calls[1]
    assert calls[2][-1] == STUB
    counter, charges = _quota(account_id)
    assert counter["used"] == 3
    assert len(charges) == 3
