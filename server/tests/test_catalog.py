import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from server.accounts import create_account, create_session, format_utc
from server.app import app
from server.browser_action import named_address
from server.catalog import NEED_ADDRESS, NEED_FIELDS
from server.db import create_db_engine
from server.models import accounts, catalog_apps

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
ITEM_FIELDS = ("id", "name", "address", "description", "created_at")
ROW_FIELDS = (
    "id",
    "account_id",
    "name",
    "address",
    "description",
    "created_at",
)
INVALID_REQUEST = "invalid_request"
NEED_FIELDS_BODY = {"code": INVALID_REQUEST, "message": NEED_FIELDS}
NEED_ADDRESS_BODY = {"code": INVALID_REQUEST, "message": NEED_ADDRESS}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}
UNAUTHENTICATED = {"code": "unauthenticated", "message": "Нужна сессия."}
UNCONFIRMED = {"code": "email_unconfirmed", "message": "Почта ещё не подтверждена."}
_rows_after_upgrade = -1


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


def _catalog_count() -> int:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM catalog_apps")
            ).scalar_one()
    finally:
        engine.dispose()
    return int(count)


@pytest.fixture(scope="module")
def database() -> None:
    global _rows_after_upgrade
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
    _rows_after_upgrade = _catalog_count()
    try:
        yield
    finally:
        _compose("down", "--remove-orphans", timeout=60)


def _account_items(account_id: uuid.UUID) -> list[dict]:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(catalog_apps)
                    .where(catalog_apps.c.account_id == account_id)
                    .order_by(
                        catalog_apps.c.created_at.asc(),
                        catalog_apps.c.id.asc(),
                    )
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _open_ready(email: str, *, confirmed: bool = True) -> tuple[str, uuid.UUID]:
    engine = create_db_engine()
    try:
        with engine.begin() as connection:
            account_id = create_account(connection, email, ACCEPTED)
            session_id = create_session(connection, account_id)
            if confirmed:
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == account_id)
                    .values(
                        email_confirmed_at=datetime(
                            2026, 11, 20, 12, tzinfo=timezone.utc
                        )
                    )
                )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _client(cookie: str | None = None) -> TestClient:
    client = TestClient(app)
    if cookie is not None:
        client.cookies.set("session", cookie)
    return client


def _post(
    client: TestClient,
    payload: object,
    *,
    origin: str | None = ORIGIN,
):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/catalog", json=payload, headers=headers)


def test_upgrade_leaves_an_empty_catalog(database: None) -> None:
    assert _rows_after_upgrade == 0
    cookie, account_id = _open_ready("catalog-empty@example.com")
    with _client(cookie) as client:
        listed = client.get("/api/catalog")
    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert _account_items(account_id) == []


def test_post_adds_a_site_and_another_own_address(database: None) -> None:
    cookie, account_id = _open_ready("catalog-add@example.com")
    raw_address = "  мой https://Example.com/inbox.  "
    own_address = "  https://Mine.Example/home.  "
    with _client(cookie) as client:
        created = _post(
            client,
            {
                "name": "  Почта  ",
                "address": raw_address,
                "description": "  письма  ",
            },
        )
        own = _post(
            client,
            {
                "name": "  Свой сайт  ",
                "address": own_address,
                "description": "  короткая заметка  ",
            },
        )
        listed = client.get("/api/catalog")
    assert created.status_code == 200
    assert own.status_code == 200
    stored_address = named_address(raw_address.strip())
    stored_own = named_address(own_address.strip())
    assert stored_address == "https://example.com/inbox"
    assert stored_own == "https://mine.example/home"
    assert stored_address != raw_address.strip()
    assert created.json()["name"] == "Почта"
    assert created.json()["address"] == stored_address
    assert created.json()["description"] == "письма"
    assert tuple(created.json()) == ITEM_FIELDS
    assert own.json()["name"] == "Свой сайт"
    assert own.json()["address"] == stored_own
    assert own.json()["description"] == "короткая заметка"
    assert tuple(own.json()) == ITEM_FIELDS
    for forbidden in ("type", "own", "oauth", "provider", "api"):
        assert forbidden not in created.json()
        assert forbidden not in own.json()
    stored = _account_items(account_id)
    by_id = {row["id"]: row for row in stored}
    first = by_id[uuid.UUID(created.json()["id"])]
    second = by_id[uuid.UUID(own.json()["id"])]
    assert len(stored) == 2
    assert tuple(first) == ROW_FIELDS
    assert tuple(second) == ROW_FIELDS
    assert first["account_id"] == second["account_id"] == account_id
    assert first["name"] == "Почта"
    assert first["address"] == stored_address
    assert first["description"] == "письма"
    assert second["name"] == "Свой сайт"
    assert second["address"] == stored_own
    assert second["description"] == "короткая заметка"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [
        str(row["id"]) for row in stored
    ]
    assert [item["address"] for item in listed.json()["items"]] == [
        row["address"] for row in stored
    ]
    assert listed.json()["items"][0]["created_at"] == format_utc(
        stored[0]["created_at"]
    )
    assert listed.json()["items"][1]["created_at"] == format_utc(
        stored[1]["created_at"]
    )
    assert created.json()["created_at"] == format_utc(first["created_at"])
    assert own.json()["created_at"] == format_utc(second["created_at"])


def test_empty_or_non_string_fields_create_nothing(database: None) -> None:
    cookie, account_id = _open_ready("catalog-blank@example.com")
    payloads: tuple[object, ...] = (
        {},
        {"name": "", "address": "https://example.com", "description": "описание"},
        {"name": "   ", "address": "https://example.com", "description": "описание"},
        {"name": "Сайт", "address": "", "description": "описание"},
        {"name": "Сайт", "address": "   ", "description": "описание"},
        {"name": "Сайт", "address": "https://example.com", "description": ""},
        {"name": "Сайт", "address": "https://example.com", "description": "   "},
        {"name": None, "address": "https://example.com", "description": "описание"},
        {"name": 1, "address": "https://example.com", "description": "описание"},
        {"name": "Сайт", "address": None, "description": "описание"},
        {
            "name": "Сайт",
            "address": ["https://example.com"],
            "description": "описание",
        },
        {"name": "Сайт", "address": "https://example.com", "description": None},
        {"name": "Сайт", "address": "https://example.com"},
        {"name": " ", "address": "не адрес", "description": "описание"},
        [],
        "сайт",
    )
    with _client(cookie) as client:
        for payload in payloads:
            refused = _post(client, payload)
            assert refused.status_code == 400
            assert refused.json() == NEED_FIELDS_BODY
        broken = client.post(
            "/api/catalog",
            content=b"{",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
    assert broken.status_code == 400
    assert broken.json() == NEED_FIELDS_BODY
    assert _account_items(account_id) == []


def test_a_non_address_creates_nothing(database: None) -> None:
    cookie, account_id = _open_ready("catalog-address@example.com")
    with _client(cookie) as client:
        for address in ("example.com", "не адрес", "ftp://example.com/a"):
            refused = _post(
                client,
                {
                    "name": "Сайт",
                    "address": address,
                    "description": "описание",
                },
            )
            assert refused.status_code == 400
            assert refused.json() == NEED_ADDRESS_BODY
    assert _account_items(account_id) == []


def test_another_account_does_not_see_the_item(database: None) -> None:
    owner_cookie, owner_id = _open_ready("catalog-owner@example.com")
    other_cookie, other_id = _open_ready("catalog-other@example.com")
    with _client(owner_cookie) as owner:
        created = _post(
            owner,
            {
                "name": "Почта",
                "address": "https://example.com/inbox",
                "description": "письма",
            },
        )
    with _client(other_cookie) as other:
        hidden = other.get("/api/catalog")
    with _client(owner_cookie) as owner:
        visible = owner.get("/api/catalog")
    assert created.status_code == 200
    assert hidden.status_code == 200
    assert hidden.json() == {"items": []}
    assert _account_items(other_id) == []
    assert [item["id"] for item in visible.json()["items"]] == [created.json()["id"]]
    assert _account_items(owner_id)[0]["account_id"] == owner_id


def test_post_without_origin_creates_nothing(database: None) -> None:
    cookie, account_id = _open_ready("catalog-origin@example.com")
    payload = {
        "name": "Почта",
        "address": "https://example.com/inbox",
        "description": "письма",
    }
    with _client(cookie) as client:
        missing = _post(client, payload, origin=None)
        foreign = _post(client, payload, origin="http://evil.example")
        listed = client.get("/api/catalog")
    assert missing.status_code == 403
    assert foreign.status_code == 403
    assert missing.json() == foreign.json() == INVALID_ORIGIN
    assert listed.status_code == 200
    assert listed.json() == {"items": []}
    assert _account_items(account_id) == []


def test_catalog_without_a_session_is_rejected(database: None) -> None:
    cookie, account_id = _open_ready(
        "catalog-unconfirmed@example.com", confirmed=False
    )
    payload = {
        "name": "Почта",
        "address": "https://example.com/inbox",
        "description": "письма",
    }
    with _client() as anonymous:
        missing_list = anonymous.get("/api/catalog")
        missing_post = _post(anonymous, payload)
    with _client(cookie) as unconfirmed:
        closed_list = unconfirmed.get("/api/catalog")
        closed_post = _post(unconfirmed, payload)
    assert missing_list.status_code == 401
    assert missing_post.status_code == 401
    assert missing_list.json() == missing_post.json() == UNAUTHENTICATED
    assert closed_list.status_code == 403
    assert closed_post.status_code == 403
    assert closed_list.json() == closed_post.json() == UNCONFIRMED
    assert _account_items(account_id) == []


def _route_entries() -> list[tuple[str, frozenset[str]]]:
    entries: list[tuple[str, frozenset[str]]] = []
    for route in app.routes:
        included = getattr(route, "original_router", None)
        children = included.routes if included is not None else (route,)
        for child in children:
            path = getattr(child, "path", "") or ""
            methods = frozenset(getattr(child, "methods", ()) or ())
            entries.append((path, methods))
    return entries


def test_routes_have_no_oauth_provider_or_site_api() -> None:
    entries = _route_entries()
    oauth_paths = [path for path, _methods in entries if "oauth" in path.lower()]
    assert oauth_paths == ["/docs/oauth2-redirect"]
    for path, _methods in entries:
        lowered = path.lower()
        assert "provider" not in lowered
        assert "site-api" not in lowered
        assert "/site" not in lowered
        if lowered.startswith("/api"):
            assert "oauth" not in lowered
    catalog = [methods for path, methods in entries if path == "/api/catalog"]
    assert catalog
    assert {path for path, _methods in entries if "catalog" in path} == {
        "/api/catalog"
    }
    methods: set[str] = set()
    for route_methods in catalog:
        methods.update(route_methods)
    assert "GET" in methods
    assert "POST" in methods
    assert methods <= {"GET", "POST", "HEAD"}
