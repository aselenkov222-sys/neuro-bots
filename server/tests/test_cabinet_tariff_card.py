import re
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import app

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
REMOVED = (
    "библиотек",
    "Библиотек",
    "Главный",
    "Рабочий десктоп",
    "Оплатить",
    "xlsx",
    'data-action="pay"',
    "pick-tariff",
    "main-toggle",
    "Выбор тарифа",
    "Тариф 1",
    "Тариф 2",
    "До первой оплаты",
    'from: "file"',
    'from: "gate"',
)


def _screen(script: str, name: str, follower: str) -> str:
    return script.split(f"function {name}(", 1)[1].split(follower, 1)[0]


def test_cabinet_drops_later_features_and_shows_one_tariff_card() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert 'href="styles.css"' in page.text
        assert page.text.count('rel="stylesheet"') == 2

        styles = client.get("/styles.css")
        assert styles.status_code == 200
        assert styles.content == (WEB_DIR / "styles.css").read_bytes()

        script_response = client.get("/app.js")
        assert script_response.status_code == 200
        script = script_response.text
        assert script == (WEB_DIR / "app.js").read_text(encoding="utf-8")
        for piece in REMOVED:
            assert piece not in script
        assert "500" not in script

        tariff = _screen(script, "tariffScreen", "function registerScreen(")
        assert tariff.count('class="tariff') == 1
        assert 'topbar("Тариф"' in tariff
        assert "Выбор тарифа" not in tariff
        assert "500" not in tariff
        assert re.search(r"\b5\b", tariff) is None
        assert "price" not in tariff
        assert "radio" not in tariff

        register = _screen(script, "registerScreen", "function loginScreen(")
        login = _screen(script, "loginScreen", "function botCard(")
        assert "Почта" in register and "Пароль" in register
        assert "Зарегистрироваться" in register and 'href="#login"' in register
        assert "Почта" in login and "Пароль" in login
        assert "Войти" in login and 'href="#register"' in login

        bots = _screen(script, "botsScreen", "function routineLocked(")
        assert 'id="create-name"' in bots
        assert 'id="create-desc"' in bots
        assert 'id="create-scenario"' in bots
        assert 'picker("create"' in bots

        card = _screen(script, "routineBlock", "function messageHtml(")
        assert 'id="bot-name"' in card
        assert 'id="bot-desc"' in card
        assert 'id="scenario"' in card
        assert 'picker("card"' in card
        assert "${routineBlock()}" in card
        assert "${day.short}" in card
        days = script.split("const DAYS = [", 1)[1].split("];", 1)[0]
        shown = days + card
        for label in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"):
            assert label in shown
        assert 'id="routine-time"' in card
        assert "Пауза" in card
        assert "Проверить сейчас" in card
        assert 'data-action="check-now"' in card
        assert "Своего текста у рутины нет." in card
        start = script.index(card)
        remainder = script[:start] + script[start + len(card):]
        for piece in ("рутин", "Рутин"):
            assert piece not in remainder
        assert "pick-color" in script
        assert "pick-icon" in script
        assert "#c85a12" in script
        assert "Искра" in script
        assert 'icon: "spark"' in script

        chat = _screen(script, "chatScreen", "function render(")
        assert 'id="composer"' in chat
        assert 'data-form="send"' in chat
        assert 'type="text"' in chat
        assert "Новый чат" in chat
        assert "Основной чат. Его нельзя удалить." in chat
        assert "Дополнительный чат. История основного сюда не копируется." in chat
        assert "Чат пустой. История из основного сюда не копируется." in chat
        assert 'data-action="delete-chat"' in chat
        for piece in ("рутин", "Рутин", "Проверить сейчас"):
            assert piece not in tariff
            assert piece not in bots
            assert piece not in chat
