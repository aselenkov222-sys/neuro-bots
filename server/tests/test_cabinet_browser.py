import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ACCEPTED = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."


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
    assert predicate(journal.text()), "server log did not reach the expected state"


def _health_ok(client: httpx.Client, origin: str) -> bool:
    try:
        response = client.get(f"{origin}/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _confirmation_urls(text: str, email: str) -> list[str]:
    suffix = f" {email}"
    found = []
    for line in text.splitlines():
        if "/api/confirm-email?token=" in line and line.endswith(suffix):
            found.append(line[: -len(suffix)])
    return found


def test_browser_registers_confirms_and_sends_one_stub_message(database: None) -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    email = f"cabinet-{uuid.uuid4()}@example.com"
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
                lambda text: "Application startup complete." in text and _health_ok(client, origin),
            )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.goto(f"{origin}/")
            page.wait_for_selector("h1")
            assert page.locator("h1").inner_text() == "Регистрация"
            page.fill("#register-email", email)
            page.fill("#register-password", ACCEPTED)
            page.click("button:has-text('Зарегистрироваться')")
            page.wait_for_selector("text=Прислать новую")
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.locator("text=Создать бота").count() == 0

            page.goto(f"{origin}/#bots")
            page.wait_for_selector("text=Прислать новую")
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.locator("text=Создать бота").count() == 0

            _wait_until(journal, lambda text: len(_confirmation_urls(text, email)) >= 1)
            before = len(_confirmation_urls(journal.text(), email))
            page.click("text=Прислать новую")
            _wait_until(
                journal,
                lambda text: len(_confirmation_urls(text, email)) > before,
            )
            link = _confirmation_urls(journal.text(), email)[-1]
            page.goto(link)
            page.wait_for_selector("text=Создать бота")
            assert page.locator("h1").inner_text() == "Боты"
            assert page.locator(".bot-card").count() == 0

            page.click("text=Создать бота")
            page.fill("#create-name", "Справочник")
            page.click("#save-bot")
            page.wait_for_selector(".bot-card")
            assert page.locator(".bot-card").inner_text().find("Справочник") >= 0
            page.click(".bot-card")
            page.wait_for_selector("text=Карточка бота")
            assert page.locator("h1").inner_text() == "Справочник"
            page.click("text=В чат")
            page.wait_for_selector("#composer")
            page.fill("#composer", "Один вопрос")
            page.click("button[aria-label='Отправить']")
            page.wait_for_selector(f"text={STUB}")
            assert page.locator(".bubble").filter(has_text=STUB).count() == 1
            browser.close()
    finally:
        _stop(process, journal)


def test_browser_shows_message_and_logout(database: None) -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    email = f"cabinet-{uuid.uuid4()}@example.com"
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
                lambda text: "Application startup complete." in text and _health_ok(client, origin),
            )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.goto(f"{origin}/#login")
            page.wait_for_selector("h1")
            assert page.locator("h1").inner_text() == "Вход"
            assert page.locator("button", has_text="Выход").count() == 0
            page.fill("#login-email", f"missing-{uuid.uuid4()}@example.com")
            page.fill("#login-password", ACCEPTED)
            page.click("button:has-text('Войти')")
            page.wait_for_selector("[role='status']")
            assert page.locator("h1").inner_text() == "Вход"
            assert page.locator("[role='status']").inner_text() == "Почта или пароль не сходятся"
            assert "invalid_credentials" not in page.locator("[role='status']").inner_text()

            page.click("text=Регистрация")
            page.wait_for_selector("#register-email")
            assert page.locator("button", has_text="Выход").count() == 0
            page.fill("#register-email", email)
            page.fill("#register-password", ACCEPTED)
            page.click("button:has-text('Зарегистрироваться')")
            page.wait_for_selector("text=Прислать новую")
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.locator("button", has_text="Выход").count() == 1

            _wait_until(journal, lambda text: len(_confirmation_urls(text, email)) >= 1)
            link = _confirmation_urls(journal.text(), email)[-1]
            page.goto(link)
            page.wait_for_selector("text=Создать бота")
            assert page.locator("h1").inner_text() == "Боты"
            assert page.locator("button", has_text="Выход").count() == 1

            page.click("text=Создать бота")
            page.fill("#create-name", "Заметки")
            page.click("#save-bot")
            page.wait_for_selector(".bot-card")
            page.click(".bot-card")
            page.wait_for_selector("text=Карточка бота")
            assert page.locator("button", has_text="Выход").count() == 1
            page.click("text=В чат")
            page.wait_for_selector("#composer")
            assert page.locator("button", has_text="Выход").count() == 1

            page.goto(link)
            page.wait_for_selector("[role='status']")
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.locator("[role='status']").inner_text() == "Ссылка больше не действует"
            assert page.locator("text=Создать бота").count() == 0
            page.wait_for_timeout(500)
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.locator("[role='status']").inner_text() == "Ссылка больше не действует"
            assert page.locator("text=Создать бота").count() == 0

            page.click("button:has-text('Выход')")
            page.wait_for_selector("text=Войти")
            assert page.locator("h1").inner_text() == "Вход"
            page.goto(f"{origin}/")
            page.wait_for_selector("h1")
            assert page.locator("h1").inner_text() == "Регистрация"
            assert page.locator("text=Создать бота").count() == 0

            page.goto(link)
            page.wait_for_selector("[role='status']")
            assert page.locator("h1").inner_text() == "Вход"
            assert page.locator("[role='status']").inner_text() == "Ссылка больше не действует"
            browser.close()
    finally:
        _stop(process, journal)


def test_browser_shows_routine_days_and_one_time(database: None) -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    email = f"cabinet-{uuid.uuid4()}@example.com"
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
                lambda text: "Application startup complete." in text and _health_ok(client, origin),
            )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.goto(f"{origin}/")
            page.wait_for_selector("h1")
            assert page.locator("h1").inner_text() == "Регистрация"
            page.fill("#register-email", email)
            page.fill("#register-password", ACCEPTED)
            page.click("button:has-text('Зарегистрироваться')")
            page.wait_for_selector("text=Прислать новую")
            _wait_until(journal, lambda text: len(_confirmation_urls(text, email)) >= 1)
            page.goto(_confirmation_urls(journal.text(), email)[-1])
            page.wait_for_selector("text=Создать бота")

            page.click("text=Создать бота")
            page.fill("#create-name", "Пустой")
            page.click("#save-bot")
            page.wait_for_selector(".bot-card")
            page.click(".bot-card")
            page.wait_for_selector("#routine-time")
            assert page.locator("#routine-locked").inner_text() == "Сначала заполните сценарий."
            assert page.locator(".day").count() == 7
            assert page.locator(".day").first.is_disabled()
            assert page.locator("#routine-time").is_disabled()
            assert page.locator("[data-action='toggle-pause']").is_disabled()

            page.goto(f"{origin}/#bots")
            page.wait_for_selector("text=Создать бота")
            page.click("text=Создать бота")
            page.fill("#create-name", "Искра")
            page.fill("#create-scenario", "план")
            page.click("#save-bot")
            page.locator(".bot-card", has_text="Искра").click()
            page.wait_for_selector("#routine-time")
            labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
            days = page.locator(".day")
            assert days.count() == 7
            for index, label in enumerate(labels):
                assert days.nth(index).inner_text() == label
                assert days.nth(index).is_enabled()
            assert page.locator("#routine-time").is_enabled()
            assert page.locator("#routine-time").input_value() == "09:00"
            assert page.locator("[data-action='toggle-pause']").inner_text() == "Пауза"
            assert page.locator("#routine-locked").count() == 0
            assert (
                page.locator("text=Дни недели и одно время. Своего текста у рутины нет.").count()
                == 1
            )
            assert page.locator("text=Проверить сейчас").count() == 0
            assert page.locator("text=Кликабельный макет").count() == 0
            assert page.locator(".proto").count() == 0
            assert page.locator(".proto-nav").count() == 0
            assert page.locator("h1").inner_text() == "Искра"

            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/routines")
                and response.ok
            ):
                page.locator(".day", has_text="Пн").click()
            assert page.locator(".day.is-on").inner_text() == "Пн"
            with page.expect_response(
                lambda response: response.request.method == "PATCH"
                and "/routines/" in response.url
                and response.ok
            ):
                page.fill("#routine-time", "10:30")
            assert page.locator("#routine-time").input_value() == "10:30"
            with page.expect_response(
                lambda response: response.request.method == "PATCH"
                and "/routines/" in response.url
                and response.ok
            ):
                page.locator("[data-action='toggle-pause']").click()
            assert page.locator("[data-action='toggle-pause']").inner_text() == "Снять паузу"
            with page.expect_response(
                lambda response: response.request.method == "DELETE"
                and "/routines/" in response.url
                and response.ok
            ):
                page.locator(".day.is-on").click()
            assert page.locator(".day.is-on").count() == 0
            browser.close()
    finally:
        _stop(process, journal)
