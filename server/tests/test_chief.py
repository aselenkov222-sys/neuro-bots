import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.accounts import create_account, create_session
from server.app import app
from server.db import create_db_engine
from server.models import accounts, bots

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
ORIGIN = "http://testserver"
ACCEPTED = "abcdef1!"
INVALID_CHIEF = {"code": "invalid_request", "message": "Нужен признак главного."}
NOT_FOUND = {"code": "not_found", "message": "Бот не найден."}
INVALID_ORIGIN = {
    "code": "invalid_origin",
    "message": "Запрос пришёл не с нашего сервера.",
}


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
                    email_confirmed_at=datetime(2026, 11, 16, tzinfo=timezone.utc)
                )
            )
    finally:
        engine.dispose()
    return str(session_id), account_id


def _client(cookie: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", cookie)
    return client


def _create(client: TestClient, name: str) -> dict:
    response = client.post(
        "/api/bots",
        json={"name": name, "chief": True},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _stored_chief(bot_id: str) -> bool:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            value = connection.execute(
                select(bots.c.chief).where(bots.c.id == uuid.UUID(bot_id))
            ).scalar_one()
    finally:
        engine.dispose()
    return value is True


def test_the_card_assigns_one_chief_and_can_clear_it(database: None) -> None:
    cookie, _account_id = _open_confirmed("chief-one@example.com")
    other_cookie, _other = _open_confirmed("chief-other@example.com")
    with _client(cookie) as client:
        first = _create(client, "Почта")
        second = _create(client, "Таблица")
        assert first["chief"] is False
        assert second["chief"] is False
        assert _stored_chief(first["id"]) is False
        named = client.patch(
            f"/api/bots/{first['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert named.status_code == 200
        assert named.json()["chief"] is True
        assert named.json()["scenario"] == first["scenario"]
        assert named.json()["name"] == "Почта"
        assert _stored_chief(first["id"]) is True
        replaced = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert replaced.status_code == 200
        assert replaced.json()["chief"] is True
        listed = client.get("/api/bots")
        assert listed.status_code == 200
        by_id = {bot["id"]: bot["chief"] for bot in listed.json()["bots"]}
        assert by_id == {first["id"]: False, second["id"]: True}
        assert _stored_chief(first["id"]) is False
        assert _stored_chief(second["id"]) is True
        again = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert again.status_code == 200
        assert again.json()["chief"] is True
        cleared = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": False},
            headers={"Origin": ORIGIN},
        )
        assert cleared.status_code == 200
        assert cleared.json()["chief"] is False
        assert _stored_chief(second["id"]) is False
        kept = client.patch(
            f"/api/bots/{first['id']}",
            json={"name": "Почта 2"},
            headers={"Origin": ORIGIN},
        )
        assert kept.status_code == 200
        assert kept.json()["chief"] is False
        refused = client.patch(
            f"/api/bots/{first['id']}",
            json={"chief": "yes"},
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 400
        assert refused.json() == INVALID_CHIEF
        missing = client.patch(
            f"/api/bots/{uuid.uuid4()}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert missing.status_code == 404
        assert missing.json() == NOT_FOUND
        foreign = client.patch(
            f"/api/bots/{second['id']}",
            json={"chief": True},
        )
        assert foreign.status_code == 403
        assert foreign.json() == INVALID_ORIGIN
    with _client(other_cookie) as other:
        theirs = _create(other, "Свой")
        appointed = other.patch(
            f"/api/bots/{theirs['id']}",
            json={"chief": True},
            headers={"Origin": ORIGIN},
        )
        assert appointed.status_code == 200
        assert appointed.json()["chief"] is True
        stolen = other.patch(
            f"/api/bots/{second['id']}",
            json={"chief": False},
            headers={"Origin": ORIGIN},
        )
        assert stolen.status_code == 404
    assert _stored_chief(theirs["id"]) is True
    assert _stored_chief(second["id"]) is False


def test_cabinet_assigns_the_chief_on_the_bot_card() -> None:
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    styles = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    card = script.split("function botScreen(", 1)[1].split("function accessSite(", 1)[0]
    bots = script.split("function botCard(", 1)[1].split("function botsScreen(", 1)[0]
    chat = script.split("function chatScreen(", 1)[1].split("function render(", 1)[0]
    assert 'id="main-toggle"' in card
    assert "Главный" in card
    assert "bot.chief === true" in card
    assert 'class="chief">Главный' in bots
    assert 'class="chief">Главный' in chat
    assert 'id="main-toggle"' in script
    assert "patchBot({ chief: target.checked })" in script
    assert ".switch" in styles
    assert ".chief" in styles
