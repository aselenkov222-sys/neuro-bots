import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import fields
from datetime import datetime, timedelta, timezone
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
from server.auth import begin
from server.browser_action import enqueue_open, finish_open
from server.config import (
    configured_tariff_name,
    configured_tariff_price,
    development_tariff,
    payment_required,
)
from server.confirmation import issue_confirmation_link
from server.db import create_db_engine
from server.models import (
    accounts,
    chats,
    messages,
    quota_charges,
    quota_counters,
    routine_checks,
    routine_runs,
    subscription_periods,
)
from server.payment import apply_payment_event
from server.routines import settle_due

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
DUE = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
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
PROVIDER_NAMES = (
    "stripe",
    "yookassa",
    "paypal",
    "cloudpayments",
    "tinkoff",
    "robokassa",
)
INACTIVE_TEXT = "Подписка неактивна."
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


def _counters(account_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(quota_counters)
                    .join(
                        subscription_periods,
                        subscription_periods.c.id == quota_counters.c.period_id,
                    )
                    .where(subscription_periods.c.account_id == account_id)
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


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


def test_absent_name_and_price_confirm_a_development_period(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    field_names = {field.name for field in fields(development_tariff)}
    assert development_tariff.model_requests == 500
    assert development_tariff.routines == 5
    assert field_names == {"model_requests", "routines"}
    moment = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("server.auth.utc_now", lambda: moment)
    for name, price in ((None, None), ("", "")):
        _price_env(monkeypatch, name, price)
        assert configured_tariff_name() is None
        assert configured_tariff_price() is None
        assert payment_required() is False
        email = f"pay-dev-{uuid.uuid4()}@example.com"
        account_id, cookie = _open_account(email, None)
        with begin() as connection:
            token = issue_confirmation_link(connection, account_id)
        with _client(cookie) as client:
            confirmed = client.get("/api/confirm-email", params={"token": token})
            me = client.get("/api/me")
            refused = _pay(client, {})
        assert confirmed.status_code == 200
        assert confirmed.json()["email_confirmed"] is True
        stored = _periods(account_id)
        assert len(stored) == 1
        assert stored[0]["active"] is True
        assert stored[0]["started_at"] == moment
        assert stored[0]["ends_at"] == calendar_month_end(moment)
        assert _used(stored[0]["id"]) == 0
        assert me.status_code == 200
        assert me.json()["tariff"] == {"name": None, "price": None}
        assert me.json()["quota_counter"] == 0
        assert refused.status_code == 400
        assert refused.json() == PRICE_UNSET
        assert _periods(account_id) == stored


def test_priced_confirmation_leaves_the_period_to_payment(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _price_env(monkeypatch, TARIFF_NAME, TARIFF_PRICE)
    moment = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("server.auth.utc_now", lambda: moment)
    monkeypatch.setattr("server.payment.utc_now", lambda: moment)
    email = f"pay-confirm-{uuid.uuid4()}@example.com"
    account_id, cookie = _open_account(email, None)
    with begin() as connection:
        token = issue_confirmation_link(connection, account_id)
    with _client(cookie) as client:
        confirmed = client.get("/api/confirm-email", params={"token": token})
        me = client.get("/api/me")
    assert confirmed.status_code == 200
    assert confirmed.json() == {"email_confirmed": True, "destination": "tariff"}
    assert me.status_code == 200
    assert me.json()["email_confirmed"] is True
    assert me.json()["period"] is None
    assert _periods(account_id) == []
    assert _counters(account_id) == []
    with _client(cookie) as client:
        paid = _pay(client, {})
    assert paid.status_code == 200
    stored = _periods(account_id)
    assert len(stored) == 1
    assert stored[0]["active"] is True
    assert stored[0]["started_at"] == moment
    assert stored[0]["ends_at"] == calendar_month_end(moment)
    assert _used(stored[0]["id"]) == 0
    assert len(_counters(account_id)) == 1
    assert paid.json()["period"] == {
        "started_at": format_utc(moment),
        "ends_at": format_utc(calendar_month_end(moment)),
        "active": True,
    }
    assert paid.json()["quota_counter"] == 0
    stranger = f"pay-confirm-anon-{uuid.uuid4()}@example.com"
    stranger_id, _cookie = _open_account(stranger, None)
    with begin() as connection:
        stranger_token = issue_confirmation_link(connection, stranger_id)
    with _client() as anonymous:
        opened = anonymous.get(
            "/api/confirm-email", params={"token": stranger_token}
        )
    assert opened.status_code == 200
    assert opened.json() == {"email_confirmed": True, "destination": "login"}
    assert _periods(stranger_id) == []
    assert _counters(stranger_id) == []


def test_one_tariff_value_still_confirms_a_development_period(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    moment = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("server.auth.utc_now", lambda: moment)
    cases = (
        (TARIFF_NAME, None),
        (TARIFF_NAME, ""),
        (None, TARIFF_PRICE),
        ("", TARIFF_PRICE),
    )
    for name, price in cases:
        _price_env(monkeypatch, name, price)
        assert payment_required() is False
        email = f"pay-half-{uuid.uuid4()}@example.com"
        account_id, cookie = _open_account(email, None)
        with begin() as connection:
            token = issue_confirmation_link(connection, account_id)
        with _client(cookie) as client:
            confirmed = client.get("/api/confirm-email", params={"token": token})
        assert confirmed.status_code == 200
        assert confirmed.json() == {"email_confirmed": True, "destination": "bots"}
        stored = _periods(account_id)
        assert len(stored) == 1
        assert stored[0]["active"] is True
        assert stored[0]["started_at"] == moment
        assert stored[0]["ends_at"] == calendar_month_end(moment)
        assert _used(stored[0]["id"]) == 0
        assert len(_counters(account_id)) == 1


def _refuse_aggregator(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_complete(*args: object, **kwargs: object) -> str:
        raise AssertionError("aggregator was called")

    monkeypatch.setattr("server.aggregator.complete", fail_complete)


def _settle(moment: datetime) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            settle_due(connection, moment)
    finally:
        engine.dispose()


def _runs_for(routine_id: str) -> list[dict]:
    parsed = uuid.UUID(routine_id)
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(routine_runs).where(routine_runs.c.routine_id == parsed)
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _chat_messages(bot_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            chat_ids = (
                connection.execute(
                    select(chats.c.id).where(chats.c.bot_id == bot_id)
                )
                .scalars()
                .all()
            )
            rows = (
                connection.execute(
                    select(messages).where(messages.c.chat_id.in_(chat_ids))
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _prepare_due_routine(
    email: str,
) -> tuple[str, uuid.UUID, uuid.UUID, dict, dict]:
    started = datetime(2026, 11, 16, tzinfo=timezone.utc)
    account_id, cookie = _open_account(email, started)
    _insert_open_period(account_id, started, calendar_month_end(started))
    computer = uuid.uuid4()
    with _client(cookie) as client:
        created_bot = client.post(
            "/api/bots",
            json={"name": "Искра", "scenario": "план"},
            headers={"Origin": ORIGIN},
        )
        assert created_bot.status_code == 200
        bot = created_bot.json()
        created = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [1, 3, 5], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        posted = client.post(
            "/api/desktops",
            json={"id": str(computer)},
            headers={"Origin": ORIGIN},
        )
        assert posted.status_code == 200
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200
    return cookie, account_id, computer, bot, created.json()


def _pulse_at(
    cookie: str,
    computer: uuid.UUID,
    moment: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("server.desktops.utc_now", lambda: moment)
    with _client(cookie) as client:
        pulsed = client.post(
            f"/api/desktops/{computer}/pulse",
            json={"timezone": "Europe/Moscow"},
            headers={"Origin": ORIGIN},
        )
        assert pulsed.status_code == 200


def _set_ends_at(account_id: uuid.UUID, ends_at: datetime) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                subscription_periods.update()
                .where(subscription_periods.c.account_id == account_id)
                .values(ends_at=ends_at)
            )
    finally:
        engine.dispose()


def _insert_open_period(
    account_id: uuid.UUID, started_at: datetime, ends_at: datetime
) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            period_id = uuid.uuid4()
            connection.execute(
                subscription_periods.insert().values(
                    id=period_id,
                    account_id=account_id,
                    started_at=started_at,
                    ends_at=ends_at,
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


def test_cancellation_before_the_end_keeps_the_period_and_opens_the_minute(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        f"cancel-open-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    stored = _periods(account_id)
    assert len(stored) == 1
    ends_at = stored[0]["ends_at"]
    assert DUE < ends_at
    with begin() as connection:
        apply_payment_event(connection, account_id, "cancellation", DUE)
    kept = _periods(account_id)
    assert len(kept) == 1
    assert kept[0]["active"] is True
    assert kept[0]["ends_at"] == ends_at
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["due_at"] == DUE
    assert _chat_messages(bot_id) == []


def test_settling_at_the_period_end_does_not_open_while_the_flag_stays_true(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        f"cancel-end-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_ends_at(account_id, DUE)
    with begin() as connection:
        apply_payment_event(connection, account_id, "cancellation", DUE - timedelta(seconds=1))
    kept = _periods(account_id)
    assert len(kept) == 1
    assert kept[0]["active"] is True
    assert kept[0]["ends_at"] == DUE
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert len(runs) == 1
    assert runs[0]["status"] != "pending"
    assert runs[0]["due_at"] == DUE
    _assert_inactive_chat(bot_id)
    assert _periods(account_id)[0]["active"] is True
    with begin() as connection:
        apply_payment_event(connection, account_id, "cancellation", DUE)
    closed = _periods(account_id)
    assert len(closed) == 1
    assert closed[0]["active"] is False
    assert closed[0]["ends_at"] == DUE


def test_failed_renewal_before_the_end_still_opens_the_minute(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        f"renew-open-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    ends_at = _periods(account_id)[0]["ends_at"]
    assert DUE < ends_at
    with begin() as connection:
        apply_payment_event(connection, account_id, "renewal_failed", DUE)
    kept = _periods(account_id)
    assert len(kept) == 1
    assert kept[0]["active"] is True
    assert kept[0]["ends_at"] == ends_at
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    opened = _runs_for(created["id"])
    assert len(opened) == 1
    assert opened[0]["status"] == "pending"
    assert opened[0]["due_at"] == DUE
    assert _chat_messages(bot_id) == []


def test_failed_renewal_at_the_end_does_not_start_the_routine(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def spy(**kwargs: object) -> str:
        calls.append(kwargs)
        return "called"

    monkeypatch.setattr("server.aggregator.complete", spy)
    cookie, account_id, computer, bot, created = _prepare_due_routine(
        f"renew-end-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_ends_at(account_id, DUE)
    with begin() as connection:
        apply_payment_event(connection, account_id, "renewal_failed", DUE)
    closed = _periods(account_id)
    assert len(closed) == 1
    assert closed[0]["active"] is False
    assert closed[0]["ends_at"] == DUE
    _pulse_at(cookie, computer, DUE, monkeypatch)
    _settle(DUE)
    runs = _runs_for(created["id"])
    assert runs
    assert all(row["status"] != "pending" for row in runs)
    assert calls == []
    _assert_inactive_chat(bot_id)


def test_missing_period_is_left_alone_by_cancellation_and_failed_renewal(
    database: None,
) -> None:
    email = f"pay-empty-{uuid.uuid4()}@example.com"
    account_id, _cookie = _open_account(email, CONFIRMED_AT)
    with begin() as connection:
        apply_payment_event(connection, account_id, "cancellation", DUE)
        apply_payment_event(connection, account_id, "renewal_failed", DUE)
    assert _periods(account_id) == []


def test_unknown_payment_kind_does_not_insert_or_deactivate_a_period(
    database: None,
) -> None:
    started = datetime(2026, 11, 16, tzinfo=timezone.utc)
    ends_at = calendar_month_end(started)
    account_id, _cookie = _open_account(
        f"pay-kind-{uuid.uuid4()}@example.com", started
    )
    _insert_open_period(account_id, started, ends_at)
    before = _periods(account_id)
    counters = _counters(account_id)
    assert len(before) == 1
    assert before[0]["active"] is True
    with begin() as connection:
        for kind in ("refund", "", "Success"):
            apply_payment_event(connection, account_id, kind, ends_at)
    assert _periods(account_id) == before
    assert _counters(account_id) == counters
    empty_id, _cookie = _open_account(
        f"pay-kind-empty-{uuid.uuid4()}@example.com", started
    )
    with begin() as connection:
        apply_payment_event(connection, empty_id, "refund", ends_at)
    assert _periods(empty_id) == []
    assert _counters(empty_id) == []


def test_sources_do_not_name_a_payment_provider() -> None:
    roots = (PROJECT_ROOT / "src", PROJECT_ROOT.parent / "web")
    for root in roots:
        assert root.is_dir()
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            folded = text.casefold()
            for name in PROVIDER_NAMES:
                assert name.casefold() not in folded, path


def test_me_reports_inactive_at_the_period_end_without_writing_the_flag(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"pay-read-{uuid.uuid4()}@example.com"
    started = datetime(2026, 11, 16, tzinfo=timezone.utc)
    ends_at = calendar_month_end(started)
    account_id, cookie = _open_account(email, started)
    _insert_open_period(account_id, started, ends_at)
    monkeypatch.setattr("server.auth.utc_now", lambda: ends_at)
    with _client(cookie) as client:
        at_end = client.get("/api/me")
        logged_in = client.post(
            "/api/login",
            json={"email": email, "password": ACCEPTED},
            headers={"Origin": ORIGIN},
        )
    assert at_end.status_code == 200
    assert at_end.json()["period"]["active"] is False
    assert at_end.json()["period"]["ends_at"] == format_utc(ends_at)
    assert logged_in.status_code == 200
    assert logged_in.json()["period"]["active"] is False
    assert _periods(account_id)[0]["active"] is True
    monkeypatch.setattr(
        "server.auth.utc_now", lambda: ends_at + timedelta(seconds=1)
    )
    with _client(cookie) as client:
        after = client.get("/api/me")
    assert after.status_code == 200
    assert after.json()["period"]["active"] is False
    assert _periods(account_id)[0]["active"] is True
    assert _periods(account_id)[0]["ends_at"] == ends_at


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


def _set_period_active(account_id: uuid.UUID, active: bool) -> None:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                subscription_periods.update()
                .where(subscription_periods.c.account_id == account_id)
                .values(active=active)
            )
    finally:
        engine.dispose()


def _charges(account_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            period_ids = (
                connection.execute(
                    select(subscription_periods.c.id).where(
                        subscription_periods.c.account_id == account_id
                    )
                )
                .scalars()
                .all()
            )
            rows = (
                connection.execute(
                    select(quota_charges).where(
                        quota_charges.c.period_id.in_(period_ids)
                    )
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _checks(bot_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(routine_checks).where(routine_checks.c.bot_id == bot_id)
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _main_chat_id(bot_id: uuid.UUID) -> uuid.UUID:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            chat_id = connection.execute(
                select(chats.c.id).where(chats.c.bot_id == bot_id)
            ).scalar_one()
    finally:
        engine.dispose()
    return chat_id


def _assert_inactive_chat(bot_id: uuid.UUID) -> None:
    rows = _chat_messages(bot_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "error"
    assert rows[0]["code"] is None
    assert rows[0]["text"] == INACTIVE_TEXT


def _priced_bot(email: str) -> tuple[uuid.UUID, str, dict, dict]:
    started = datetime(2026, 11, 16, tzinfo=timezone.utc)
    account_id, cookie = _open_account(email, started)
    _insert_open_period(account_id, started, calendar_month_end(started))
    with _client(cookie) as client:
        created_bot = client.post(
            "/api/bots",
            json={"name": "Искра", "scenario": "план"},
            headers={"Origin": ORIGIN},
        )
        assert created_bot.status_code == 200
        bot = created_bot.json()
        created = client.post(
            f"/api/bots/{bot['id']}/routines",
            json={"weekdays": [1, 3, 5], "time": "09:00"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
    return account_id, cookie, bot, created.json()


def test_inactive_period_keeps_reads_and_edits_open(database: None) -> None:
    account_id, cookie, bot, routine = _priced_bot(
        f"pay-read-edit-{uuid.uuid4()}@example.com"
    )
    _set_period_active(account_id, False)
    assert _periods(account_id)[0]["active"] is False
    with _client(cookie) as client:
        loaded = client.get(f"/api/bots/{bot['id']}")
        spoken = client.get(f"/api/bots/{bot['id']}/messages")
        library = client.get(f"/api/bots/{bot['id']}/library")
        renamed = client.patch(
            f"/api/bots/{bot['id']}",
            json={"name": "Новая", "scenario": "другой план"},
            headers={"Origin": ORIGIN},
        )
        rescheduled = client.patch(
            f"/api/bots/{bot['id']}/routines/{routine['id']}",
            json={"weekdays": [2, 4], "time": "10:30"},
            headers={"Origin": ORIGIN},
        )
        profile = client.patch(
            "/api/me",
            json={"name": "Аня"},
            headers={"Origin": ORIGIN},
        )
    assert loaded.status_code == 200
    assert loaded.json()["name"] == "Искра"
    assert spoken.status_code == 200
    assert spoken.json()["messages"] == []
    assert library.status_code == 200
    assert library.json()["files"] == []
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Новая"
    assert renamed.json()["scenario"] == "другой план"
    assert rescheduled.status_code == 200
    assert rescheduled.json()["weekdays"] == [2, 4]
    assert rescheduled.json()["time"] == "10:30"
    assert profile.status_code == 200
    assert profile.json()["account"]["name"] == "Аня"
    assert _used(_periods(account_id)[0]["id"]) == 0
    assert _charges(account_id) == []


def test_inactive_message_saves_the_user_and_skips_the_model(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    account_id, cookie, bot, _routine = _priced_bot(
        f"pay-message-{uuid.uuid4()}@example.com"
    )
    _set_period_active(account_id, False)
    proposal = "сохрани в сценарий: новый план"
    with _client(cookie) as client:
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": proposal},
            headers={"Origin": ORIGIN},
        )
        kept = client.get(f"/api/bots/{bot['id']}")
    assert posted.status_code == 200
    user, error = posted.json()["messages"]
    assert user["role"] == "user"
    assert user["text"] == proposal
    assert error["role"] == "error"
    assert error["code"] is None
    assert error["text"] == INACTIVE_TEXT
    assert kept.json()["scenario"] == "план"
    assert _used(_periods(account_id)[0]["id"]) == 0
    assert _charges(account_id) == []


def test_ended_period_with_the_flag_still_true_does_not_charge_a_message(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    account_id, cookie, bot, _routine = _priced_bot(
        f"pay-ended-message-{uuid.uuid4()}@example.com"
    )
    moment = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    _set_ends_at(account_id, moment)
    assert _periods(account_id)[0]["active"] is True
    monkeypatch.setattr("server.bots.utc_now", lambda: moment)
    with _client(cookie) as client:
        posted = client.post(
            f"/api/bots/{bot['id']}/messages",
            json={"text": "ещё"},
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    user, error = posted.json()["messages"]
    assert user["role"] == "user"
    assert error["role"] == "error"
    assert error["code"] is None
    assert error["text"] == INACTIVE_TEXT
    assert _periods(account_id)[0]["active"] is True
    assert _used(_periods(account_id)[0]["id"]) == 0
    assert _charges(account_id) == []


def test_check_now_writes_inactive_and_leaves_no_check(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    account_id, cookie, bot, routine = _priced_bot(
        f"pay-check-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    _set_period_active(account_id, False)
    with _client(cookie) as client:
        posted = client.post(
            f"/api/bots/{bot['id']}/check",
            headers={"Origin": ORIGIN},
        )
    assert posted.status_code == 200
    spoken = posted.json()["messages"]
    assert len(spoken) == 1
    assert spoken[0]["role"] == "error"
    assert spoken[0]["code"] is None
    assert spoken[0]["text"] == INACTIVE_TEXT
    assert "queued" not in posted.json()
    assert _checks(bot_id) == []
    assert _runs_for(routine["id"]) == []
    assert _charges(account_id) == []
    assert _used(_periods(account_id)[0]["id"]) == 0


def test_finish_open_with_an_ended_period_does_not_call_the_model(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_aggregator(monkeypatch)
    account_id, _cookie, bot, _routine = _priced_bot(
        f"pay-open-{uuid.uuid4()}@example.com"
    )
    bot_id = uuid.UUID(bot["id"])
    moment = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    _set_ends_at(account_id, moment)
    assert _periods(account_id)[0]["active"] is True
    chat_id = _main_chat_id(bot_id)
    computer = uuid.uuid4()
    with begin() as connection:
        connection.execute(
            messages.insert().values(
                id=uuid.uuid4(),
                chat_id=chat_id,
                role="user",
                text="открой",
                code=None,
                updated_at=moment - timedelta(seconds=1),
            )
        )
        enqueue_open(
            account_id=account_id,
            computer_id=computer,
            url="https://example.com/page",
            bot_id=bot_id,
            chat_id=chat_id,
        )
        finished = finish_open(connection, account_id, computer, moment, "страница")
    assert finished.status_code == 200
    payload = json.loads(finished.body)
    assert [row["role"] for row in payload["messages"]] == ["user", "error"]
    error = payload["messages"][1]
    assert error["code"] is None
    assert error["text"] == INACTIVE_TEXT
    assert _periods(account_id)[0]["active"] is True
    assert _used(_periods(account_id)[0]["id"]) == 0
    assert _charges(account_id) == []
