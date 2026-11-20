import logging
import os
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

from server.accounts import (
    calendar_month_end,
    create_account,
    create_session,
    format_utc,
)
from server.app import app
from server.db import create_db_engine
from server.models import accounts, quota_counters, subscription_periods

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
TARIFF_NAME = "Месяц"
TARIFF_PRICE = "900 ₽"
CONFIRMED_AT = datetime(2026, 11, 16, tzinfo=timezone.utc)
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
EMAIL_UNCONFIRMED = {
    "code": "email_unconfirmed",
    "message": "Почта ещё не подтверждена.",
}
PRICE_UNSET = {"code": "invalid_request", "message": "Оплата не задана."}
INVALID_BODY = {"code": "invalid_request", "message": "Нужен JSON-объект."}
BODY_MARKER = "marker-value"
COOKIE_MARKER = "marker-cookie"


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


def _open_account(
    email: str, confirmed_at: datetime | None
) -> tuple[uuid.UUID, str]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            if confirmed_at is not None:
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == account_id)
                    .values(email_confirmed_at=confirmed_at)
                )
    finally:
        engine.dispose()
    return account_id, str(session_id)


def _client(cookie: str | None = None) -> TestClient:
    client = TestClient(app)
    if cookie is not None:
        client.cookies.set("session", cookie)
    return client


def _pay(client: TestClient, body: object = None, origin: str | None = ORIGIN):
    headers = {} if origin is None else {"Origin": origin}
    if body is None:
        return client.post("/api/payment", headers=headers)
    return client.post("/api/payment", json=body, headers=headers)


def _periods(account_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(subscription_periods)
                    .where(subscription_periods.c.account_id == account_id)
                    .order_by(
                        subscription_periods.c.started_at,
                        subscription_periods.c.id,
                    )
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _used(period_id: uuid.UUID) -> int:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            used = connection.execute(
                select(quota_counters.c.used).where(
                    quota_counters.c.period_id == period_id
                )
            ).scalar_one()
    finally:
        engine.dispose()
    return used


def _clear_periods(account_id: uuid.UUID) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            period_ids = select(subscription_periods.c.id).where(
                subscription_periods.c.account_id == account_id
            )
            connection.execute(
                quota_counters.delete().where(
                    quota_counters.c.period_id.in_(period_ids)
                )
            )
            connection.execute(
                subscription_periods.delete().where(
                    subscription_periods.c.account_id == account_id
                )
            )
    finally:
        engine.dispose()


def _price_env(monkeypatch: pytest.MonkeyPatch, name: str | None, price: str | None) -> None:
    if name is None:
        monkeypatch.delenv("NEURO_BOTS_TARIFF_NAME", raising=False)
    else:
        monkeypatch.setenv("NEURO_BOTS_TARIFF_NAME", name)
    if price is None:
        monkeypatch.delenv("NEURO_BOTS_TARIFF_PRICE", raising=False)
    else:
        monkeypatch.setenv("NEURO_BOTS_TARIFF_PRICE", price)


def _captured(caplog: pytest.LogCaptureFixture) -> str:
    captured = caplog.text
    for record in caplog.records:
        captured += record.getMessage()
        captured += repr(record.args)
    return captured


class Journal:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        assert process.stdout is not None
        self._thread = threading.Thread(
            target=self._read, args=(process.stdout,), daemon=True
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


def _wait_until(journal: Journal, predicate, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(journal.text()):
            return
        if journal.process.poll() is not None:
            break
        time.sleep(0.05)
    assert predicate(journal.text()), journal.text()[-2000:]


def test_priced_account_without_a_period_pays_into_an_empty_bot_list(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    email = f"pay-screen-{uuid.uuid4()}@example.com"
    account_id, cookie = _open_account(email, CONFIRMED_AT)
    with _client(cookie) as client:
        me = client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email_confirmed"] is True
    assert body["period"] is None
    assert body["quota"] is None
    assert body["quota_counter"] is None
    assert body["tariff"] == {"name": TARIFF_NAME, "price": TARIFF_PRICE}
    assert _periods(account_id) == []
    _clear_periods(account_id)

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["NEURO_BOTS_TARIFF_NAME"] = TARIFF_NAME
    env["NEURO_BOTS_TARIFF_PRICE"] = TARIFF_PRICE
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
        env=env,
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
            context = browser.new_context()
            context.add_cookies(
                [{"name": "session", "value": cookie, "url": origin}]
            )
            page = context.new_page()
            page.goto(f"{origin}/#bots")
            page.wait_for_selector("[data-action='pay']")
            assert page.locator("h1").inner_text() == "Тариф"
            assert page.get_by_text(TARIFF_NAME, exact=True).count() == 1
            assert page.get_by_text(TARIFF_PRICE, exact=True).count() == 1
            assert page.locator("article.tariff").count() == 1
            assert page.get_by_text("Создать бота", exact=True).count() == 0
            assert page.locator("[data-action='logout']").count() == 1
            for fragment in ("#bots", "#profile", "#chat"):
                page.goto(f"{origin}/{fragment}")
                page.wait_for_selector("[data-action='pay']")
                assert page.locator("h1").inner_text() == "Тариф"
                assert page.get_by_text(TARIFF_NAME, exact=True).count() == 1
                assert page.get_by_text(TARIFF_PRICE, exact=True).count() == 1
                assert page.get_by_text("Создать бота", exact=True).count() == 0
            with page.expect_response(
                lambda response: response.url.endswith("/api/payment")
                and response.request.method == "POST"
            ) as paid:
                page.click("[data-action='pay']")
            assert paid.value.status == 200
            page.wait_for_selector("text=Создать бота")
            assert page.locator("h1").inner_text() == "Боты"
            assert page.locator(".bot-card").count() == 0
            assert page.url.endswith("#bots")
            browser.close()
    finally:
        _stop(process, journal)
    stored = _periods(account_id)
    assert len(stored) == 1
    assert stored[0]["active"] is True
    assert _used(stored[0]["id"]) == 0


def test_payment_period_is_a_calendar_month_from_the_payment_time(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    moments = (
        datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 1, 31, 15, tzinfo=timezone.utc),
    )
    clock = {"at": moments[0]}

    def frozen() -> datetime:
        return clock["at"]

    monkeypatch.setattr("server.payment.utc_now", frozen)
    monkeypatch.setattr("server.auth.utc_now", frozen)
    for start in moments:
        clock["at"] = start
        email = f"pay-month-{uuid.uuid4()}@example.com"
        account_id, cookie = _open_account(email, CONFIRMED_AT)
        with _client(cookie) as client:
            paid = _pay(client, {})
            me = client.get("/api/me")
        assert paid.status_code == 200
        assert paid.json()["period"] == {
            "started_at": format_utc(start),
            "ends_at": format_utc(calendar_month_end(start)),
            "active": True,
        }
        assert paid.json()["quota_counter"] == 0
        assert me.json()["period"] == paid.json()["period"]
        stored = _periods(account_id)
        assert len(stored) == 1
        assert stored[0]["started_at"] == start
        assert stored[0]["ends_at"] == calendar_month_end(start)
        assert stored[0]["active"] is True
        assert _used(stored[0]["id"]) == 0


def test_second_payment_starts_a_new_period_with_zero_used(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    first = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
    second = datetime(2026, 12, 15, 11, tzinfo=timezone.utc)
    clock = {"at": first}

    def frozen() -> datetime:
        return clock["at"]

    monkeypatch.setattr("server.payment.utc_now", frozen)
    monkeypatch.setattr("server.auth.utc_now", frozen)
    email = f"pay-again-{uuid.uuid4()}@example.com"
    account_id, cookie = _open_account(email, CONFIRMED_AT)
    with _client(cookie) as client:
        opened = _pay(client, {})
        clock["at"] = second
        renewed = _pay(client, {})
        me = client.get("/api/me")
    assert opened.status_code == 200
    assert renewed.status_code == 200
    stored = _periods(account_id)
    assert len(stored) == 2
    previous = stored[0]
    current = stored[1]
    assert previous["started_at"] == first
    assert previous["active"] is False
    assert current["started_at"] == second
    assert current["ends_at"] == calendar_month_end(second)
    assert current["active"] is True
    assert _used(current["id"]) == 0
    assert [row for row in stored if row["active"]] == [current]
    assert me.json()["period"] == {
        "started_at": format_utc(second),
        "ends_at": format_utc(calendar_month_end(second)),
        "active": True,
    }
    assert me.json()["quota_counter"] == 0
    assert renewed.json() == me.json()


def test_payment_refuses_a_missing_origin_session_confirmation_or_price(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    email = f"pay-guard-{uuid.uuid4()}@example.com"
    account_id, cookie = _open_account(email, CONFIRMED_AT)
    plain = f"pay-plain-{uuid.uuid4()}@example.com"
    plain_id, plain_cookie = _open_account(plain, None)
    with _client(cookie) as client:
        missing_origin = _pay(client, {}, origin=None)
    with _client() as anonymous:
        missing_session = _pay(anonymous, {})
    with _client(plain_cookie) as client:
        unconfirmed = _pay(client, {})
    assert missing_origin.status_code == 403
    assert missing_origin.json() == INVALID_ORIGIN
    assert missing_session.status_code == 401
    assert missing_session.json() == UNAUTHENTICATED
    assert unconfirmed.status_code == 403
    assert unconfirmed.json() == EMAIL_UNCONFIRMED
    assert _periods(account_id) == []
    assert _periods(plain_id) == []

    cases = (
        (None, None),
        ("", ""),
        (TARIFF_NAME, None),
        (TARIFF_NAME, ""),
        (None, TARIFF_PRICE),
        ("", TARIFF_PRICE),
    )
    for name, price in cases:
        _price_env(monkeypatch, name, price)
        held = f"pay-unset-{uuid.uuid4()}@example.com"
        held_id, held_cookie = _open_account(held, CONFIRMED_AT)
        with _client(held_cookie) as client:
            refused = _pay(client, {})
            me = client.get("/api/me")
        assert refused.status_code == 400
        assert refused.json() == PRICE_UNSET
        assert _periods(held_id) == []
        assert me.json()["email_confirmed"] is True
        assert me.json()["quota"] is None
        assert me.json()["quota_counter"] is None
        assert me.json()["period"] == {
            "started_at": format_utc(CONFIRMED_AT),
            "ends_at": format_utc(calendar_month_end(CONFIRMED_AT)),
            "active": True,
        }
        assert me.json()["tariff"] == {
            "name": name if name else None,
            "price": price if price else None,
        }


def test_payment_rejects_password_and_cookie_keys_without_logging_them(
    database: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    email = f"pay-body-{uuid.uuid4()}@example.com"
    account_id, cookie = _open_account(email, CONFIRMED_AT)
    bodies = (
        {"password": BODY_MARKER},
        {"cookie": COOKIE_MARKER},
        {"password": BODY_MARKER, "cookie": COOKIE_MARKER},
    )
    with caplog.at_level(logging.DEBUG):
        with _client(cookie) as client:
            responses = [_pay(client, body) for body in bodies]
    assert [response.status_code for response in responses] == [400, 400, 400]
    assert [response.json() for response in responses] == [INVALID_BODY, INVALID_BODY, INVALID_BODY]
    captured = _captured(caplog)
    assert BODY_MARKER not in captured
    assert COOKIE_MARKER not in captured
    for response in responses:
        assert BODY_MARKER not in response.text
        assert COOKIE_MARKER not in response.text
    assert _periods(account_id) == []
