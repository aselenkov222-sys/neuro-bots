import re
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"


def _screen(script: str, name: str, follower: str) -> str:
    start = f"function {name}("
    body = script.split(start, 1)[1]
    return body.split(follower, 1)[0]


def _has_email_input(markup: str) -> bool:
    if 'type="email"' in markup:
        return True
    return bool(
        re.search(
            r"<label[^>]*>\s*Почта\s*<input\b[^>]*(?:name|id)=\"[^\"]*email",
            markup,
        )
    )


def test_register_and_login_screens_use_tariff_fields() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        stylesheet_tags = [
            tag
            for tag in re.findall(r"<link\b[^>]*>", html)
            if "stylesheet" in tag
        ]
        assert stylesheet_tags == [
            '<link href="https://fonts.googleapis.com/css2?family=Onest:wght@300;400;500;600;700&family=Unbounded:wght@400&display=swap" rel="stylesheet">',
            '<link rel="stylesheet" href="styles.css">',
        ]
        assert 'location.replace("#chat")' not in html

        styles = client.get("/styles.css")
        assert styles.status_code == 200
        assert styles.content == (WEB_DIR / "styles.css").read_bytes()

        script_response = client.get("/app.js")
        assert script_response.status_code == 200
        script = script_response.text
        assert 'location.hash || "#chat"' not in script
        assert 'api("/api/me")' in script
        assert 'if (!state.me) return "register"' in script
        assert 'hash === "#register"' in script
        assert 'hash === "#login"' in script
        assert "register: registerScreen" in script
        assert "login: loginScreen" in script

        register = _screen(script, "registerScreen", "function loginScreen(")
        login = _screen(script, "loginScreen", "function botCard(")
        for markup in (register, login):
            assert _has_email_input(markup)
            assert 'type="password"' in markup
            assert 'class="field"' in markup
            assert "Почта" in markup
            assert "Пароль" in markup
            assert "topbar(" in markup
            assert "sheet" in markup
            assert "btn-orange" in markup

        assert "Зарегистрироваться" in register
        assert 'href="#login"' in register
        assert ">Вход</a>" in register
        assert "Войти" in login
        assert 'href="#register"' in login
        assert ">Регистрация</a>" in login
        assert "fetch(" not in register
        assert "fetch(" not in login
