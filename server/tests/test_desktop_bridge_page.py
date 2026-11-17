import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
COMPUTER = "6f1e2d3c-4b5a-6789-8abc-def012345678"
ME = {
    "account": {
        "id": "11111111-1111-4111-8111-111111111111",
        "email": "desk@example.com",
        "name": "",
    },
    "email_confirmed": False,
    "period": None,
    "quota": None,
}


class _Desk:
    def __init__(self) -> None:
        self.signed_in = False
        self.posts: list[object] = []

    def handle(self, route) -> None:
        path = route.request.url.split("?", 1)[0]
        method = route.request.method
        if path.endswith("/api/me") and method == "GET":
            if self.signed_in:
                self._json(route, 200, ME)
            else:
                self._json(route, 401, {"code": "unauthenticated", "message": "Нужна сессия."})
            return
        if path.endswith("/api/register") and method == "POST":
            self.signed_in = True
            self._json(route, 200, ME)
            return
        if path.endswith("/api/login") and method == "POST":
            self.signed_in = True
            self._json(route, 200, ME)
            return
        if path.endswith("/api/desktops") and method == "POST":
            self.posts.append(route.request.post_data_json)
            self._json(route, 200, {"id": COMPUTER, "working": True})
            return
        if path.endswith("/api/bots") and method == "GET":
            self._json(route, 200, {"bots": []})
            return
        route.continue_()

    @staticmethod
    def _json(route, status: int, body: object) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body),
        )


def _serve() -> tuple[ThreadingHTTPServer, str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_cabinet_claims_a_desktop_only_from_the_bridge() -> None:
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "setInterval" not in script
    assert "pulse" not in script
    server, origin = _serve()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                _assert_browser_boot_does_not_claim(browser, origin)
                _assert_boot_claims_when_the_bridge_is_present(browser, origin)
                _assert_event_claims_after_boot(browser, origin)
                _assert_register_claims(browser, origin)
                _assert_login_claims(browser, origin)
            finally:
                browser.close()
    finally:
        server.shutdown()


def _open(browser, origin: str, desk: _Desk, *, bridge: str | None = None):
    context = browser.new_context()
    if bridge is not None:
        context.add_init_script(bridge)
    page = context.new_page()
    page.route("**/api/**", desk.handle)
    return context, page


def _assert_browser_boot_does_not_claim(browser, origin: str) -> None:
    desk = _Desk()
    desk.signed_in = True
    context, page = _open(browser, origin, desk)
    try:
        page.goto(f"{origin}/")
        page.wait_for_selector("h1")
        assert page.locator("h1").inner_text() == "Тариф"
        assert "рабочий десктоп" not in page.locator("body").inner_text().lower()
        assert desk.posts == []
    finally:
        context.close()


def _assert_boot_claims_when_the_bridge_is_present(browser, origin: str) -> None:
    desk = _Desk()
    desk.signed_in = True
    context, page = _open(
        browser,
        origin,
        desk,
        bridge=f"window.__neuroDesktop = {{id: {json.dumps(COMPUTER)}}};",
    )
    try:
        with page.expect_response("**/api/desktops"):
            page.goto(f"{origin}/")
        page.wait_for_selector("h1")
        assert desk.posts == [{"id": COMPUTER}]
    finally:
        context.close()


def _assert_event_claims_after_boot(browser, origin: str) -> None:
    desk = _Desk()
    desk.signed_in = True
    context, page = _open(browser, origin, desk)
    try:
        page.goto(f"{origin}/")
        page.wait_for_selector("h1")
        assert desk.posts == []
        with page.expect_response("**/api/desktops"):
            page.evaluate(
                """(id) => {
                    window.__neuroDesktop = {id};
                    window.dispatchEvent(new Event("neuro-desktop"));
                }""",
                COMPUTER,
            )
        assert desk.posts == [{"id": COMPUTER}]
    finally:
        context.close()


def _assert_register_claims(browser, origin: str) -> None:
    desk = _Desk()
    context, page = _open(
        browser,
        origin,
        desk,
        bridge=f"window.__neuroDesktop = {{id: {json.dumps(COMPUTER)}}};",
    )
    try:
        page.goto(f"{origin}/#register")
        page.wait_for_selector("#register-email")
        assert desk.posts == []
        page.fill("#register-email", "desk@example.com")
        page.fill("#register-password", "abcdef1!")
        with page.expect_response("**/api/desktops"):
            page.click("button:has-text('Зарегистрироваться')")
        page.wait_for_selector("text=Прислать новую")
        assert desk.posts == [{"id": COMPUTER}]
    finally:
        context.close()


def _assert_login_claims(browser, origin: str) -> None:
    desk = _Desk()
    context, page = _open(
        browser,
        origin,
        desk,
        bridge=f"window.__neuroDesktop = {{id: {json.dumps(COMPUTER)}}};",
    )
    try:
        page.goto(f"{origin}/#login")
        page.wait_for_selector("#login-email")
        assert desk.posts == []
        page.fill("#login-email", "desk@example.com")
        page.fill("#login-password", "abcdef1!")
        with page.expect_response("**/api/desktops"):
            page.click("button:has-text('Войти')")
        page.wait_for_selector("text=Прислать новую")
        assert desk.posts == [{"id": COMPUTER}]
    finally:
        context.close()
