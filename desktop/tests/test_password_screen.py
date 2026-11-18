import http.client
import subprocess
from urllib.parse import quote, urlencode

import pytest
from playwright.sync_api import sync_playwright

from desktop.keychain import SECURITY_PATH, read_password
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.window import main

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"
OTHER_SECRET = "machine-secret-8"
_SAVED = "Пароль сохранён на этом компьютере."
_MISSING = "Такой записи нет."


class FakeSecurity:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert command[0] == SECURITY_PATH
        operation = command[1]
        service = command[command.index("-s") + 1]
        account = command[command.index("-a") + 1]
        if operation == "add-generic-password":
            self.items[(service, account)] = command[command.index("-w") + 1]
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == "find-generic-password":
            stored = self.items.get((service, account))
            if stored is None:
                return subprocess.CompletedProcess(command, 44, "", "")
            return subprocess.CompletedProcess(command, 0, stored + "\n", "")
        raise AssertionError(operation)


@pytest.fixture
def screen():
    security = FakeSecurity()
    opened = serve_password_screen(run=security)
    try:
        yield opened, security
    finally:
        opened.close()


def test_screen_listens_on_loopback_and_reads_the_keychain_password(screen) -> None:
    opened, security = screen
    assert opened.httpd.server_address[0] == LOOPBACK
    assert opened.url == f"http://{LOOPBACK}:{opened.port}"
    status, body, headers = _post(opened, SITE, LOGIN, SECRET)
    assert status == 303
    assert _header(headers, "Location") == "/?saved=1"
    assert SECRET.encode() not in body
    page_status, page, _page_headers = _exchange(opened, "GET", "/?saved=1")
    assert page_status == 200
    assert _SAVED.encode() in page
    assert SECRET.encode() not in page
    read_status, read_body, _read_headers = _exchange(
        opened,
        "GET",
        f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
    )
    assert read_status == 200
    assert read_body.decode() == SECRET
    assert read_password(SITE, LOGIN, run=security) == SECRET
    assert security.commands[0][0] == SECURITY_PATH
    assert security.commands[0][1] == "add-generic-password"
    assert security.commands[-1][0] == SECURITY_PATH
    assert security.commands[-1][1] == "find-generic-password"


def test_page_talks_only_to_its_own_screen(screen) -> None:
    opened, _security = screen
    status, page, _headers = _exchange(opened, "GET", "/")
    assert status == 200
    text = page.decode()
    assert 'action="/passwords"' in text
    assert 'fetch("/passwords?"' in text
    assert "http:" not in text
    assert "https:" not in text
    assert "textContent = secret" not in text
    assert "innerHTML" not in text
    status, echoed, _headers = _exchange(
        opened,
        "GET",
        f"/?saved=1&password={quote(SECRET)}",
    )
    assert status == 200
    assert SECRET.encode() not in echoed


def test_cabinet_origin_does_not_receive_or_store_the_password(screen) -> None:
    opened, security = screen
    _post(opened, SITE, LOGIN, SECRET)
    cabinet = {"Origin": "http://127.0.0.1:9", "Sec-Fetch-Site": "same-site"}
    status, body, _headers = _exchange(
        opened,
        "GET",
        f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
        headers=cabinet,
    )
    assert status == 403
    assert SECRET.encode() not in body
    refused, refused_body, _headers = _post(
        opened,
        SITE,
        "Борис",
        OTHER_SECRET,
        headers=cabinet,
    )
    assert refused == 403
    assert OTHER_SECRET.encode() not in refused_body
    assert read_password(SITE, "Борис", run=security) is None
    missing, missing_body, _headers = _exchange(
        opened,
        "GET",
        f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}&password={quote(SECRET)}",
    )
    assert missing == 200
    assert missing_body.decode() == SECRET


def test_other_host_names_are_refused(screen) -> None:
    opened, _security = screen
    status, body, _headers = _exchange(
        opened,
        "GET",
        f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
        headers={"Host": f"localhost:{opened.port}"},
    )
    assert status == 403
    assert body == b""
    status, body, _headers = _exchange(
        opened,
        "GET",
        "/",
        headers={"Host": "cabinet.example"},
    )
    assert status == 403
    assert SECRET.encode() not in body


def test_missing_record_does_not_echo_a_query_password(screen) -> None:
    opened, _security = screen
    status, body, _headers = _exchange(
        opened,
        "GET",
        f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}&password={quote(SECRET)}",
    )
    assert status == 404
    assert body.decode() == _MISSING
    assert SECRET.encode() not in body


def test_browser_form_saves_on_the_loopback_screen_and_hides_the_password(screen) -> None:
    opened, security = screen
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.goto(opened.url + "/")
        page.locator('input[name="site"]').fill(SITE)
        page.locator('input[name="login"]').fill(LOGIN)
        page.locator('input[name="password"]').fill(SECRET)
        page.get_by_role("button", name="Сохранить").click()
        page.wait_for_url("**/?saved=1")
        saved = page.locator("body").inner_text()
        assert _SAVED in saved
        assert SECRET not in saved
        page.locator("#copy-site").fill(SITE)
        page.locator("#copy-login").fill(LOGIN)
        page.locator("#copy").click()
        page.locator("#copy-status").wait_for(state="visible")
        copied = page.locator("body").inner_text()
        assert SECRET not in copied
        assert (
            "Пароль скопирован." in copied
            or "Пароль остаётся на этом компьютере." in copied
        )
        browser.close()
    assert read_password(SITE, LOGIN, run=security) == SECRET


def test_null_origin_is_refused(screen) -> None:
    opened, security = screen
    status, body, _headers = _post(
        opened,
        SITE,
        LOGIN,
        SECRET,
        headers={"Origin": "null"},
    )
    assert status == 403
    assert SECRET.encode() not in body
    assert read_password(SITE, LOGIN, run=security) is None


def test_desktop_process_opens_the_password_screen_before_the_cabinet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def start() -> str:
        order.append("screen")
        return f"http://{LOOPBACK}:1"

    def open_cabinet(origin: str, on_ready=None) -> None:
        order.append(origin)

    monkeypatch.setattr("desktop.password_screen.start_password_screen", start)
    monkeypatch.setattr("desktop.window.open_cabinet", open_cabinet)
    main(["http://127.0.0.1:9/"])
    assert order == ["screen", "http://127.0.0.1:9/"]


def _post(opened, site: str, login: str, password: str, headers: dict[str, str] | None = None):
    body = urlencode({"site": site, "login": login, "password": password}).encode()
    sent = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    }
    if headers:
        sent.update(headers)
    return _exchange(opened, "POST", "/passwords", body, sent)


def _exchange(opened, method: str, path: str, body: bytes | None = None, headers=None):
    connection = http.client.HTTPConnection(LOOPBACK, opened.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        connection.close()


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return None
