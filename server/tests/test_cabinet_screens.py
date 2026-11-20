import re
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
SCREENS = ("tariff", "bots", "bot", "chat", "profile", "catalog")


def test_root_serves_cabinet_screens_without_prototype_header() -> None:
    with TestClient(app) as client:
        page = client.get("/")

        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        html = page.text
        assert html == (WEB_DIR / "index.html").read_text(encoding="utf-8")
        assert 'rel="stylesheet" href="styles.css"' in html
        assert 'src="app.js"' in html
        assert "proto-nav" not in html
        assert "Кликабельный макет" not in html
        assert not re.search(r'class="[^"]*\bproto\b', html)

        styles = client.get("/styles.css")
        assert styles.status_code == 200
        assert styles.content == (WEB_DIR / "styles.css").read_bytes()

        script_response = client.get("/app.js")
        assert script_response.status_code == 200
        assert script_response.content == (WEB_DIR / "app.js").read_bytes()
        script = script_response.text

        for name in SCREENS:
            assert f"function {name}Screen(" in script
            assert f"{name}: {name}Screen" in script
            assert f'hash === "#{name}"' in script
        assert "function passwordScreen(" not in script
        assert 'hash === "#password"' not in script
        assert "password: passwordScreen" not in script
        assert "Доступ для бота" not in script
