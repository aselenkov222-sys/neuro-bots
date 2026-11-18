import http.client
import json
import subprocess
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
from playwright.sync_api import sync_playwright

import desktop.vault as vault_module
from desktop.keychain import SECURITY_PATH, SERVICE_NAME
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.sign_in import perform
from desktop.vault import add_record, read_record_password, records

SITE = "example.com"
URL = "https://example.com/inbox"
LOGIN = "Аня"
OTHER = "Борис"
SECRET = "machine-secret-7"
OTHER_SECRET = "machine-secret-8"
ANYA = "bot-anya"
BORIS = "bot-boris"
PAGE = "Текст страницы"
AFTER = f"вошли {SECRET}"


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
            assert command[-1] == "-U"
            self.items[(service, account)] = command[command.index("-w") + 1]
            return subprocess.CompletedProcess(command, 0, "", "")
        if operation == "find-generic-password":
            assert command[-1] == "-w"
            stored = self.items.get((service, account))
            if stored is None:
                return subprocess.CompletedProcess(command, 44, "", "")
            return subprocess.CompletedProcess(command, 0, stored + "\n", "")
        raise AssertionError(operation)


class FakePage:
    def __init__(self, has_password: bool, before: str = PAGE, after: str = AFTER) -> None:
        self.has_password = has_password
        self.before = before
        self.after = after
        self.filled: list[tuple[str, str]] = []
        self.submitted = False

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self, selector)

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.after if self.submitted else self.before

    def close(self) -> None:
        return None


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        if "password" in self.selector:
            return 1 if self.page.has_password else 0
        return 1

    def fill(self, value: str) -> None:
        self.page.filled.append((self.selector, value))

    def click(self) -> None:
        self.page.submitted = True

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self.page, selector)


def _forbid_real_security(*args, **kwargs):
    raise AssertionError("real security was called")


def _finds(security: FakeSecurity) -> list[list[str]]:
    return [command for command in security.commands if command[1] == "find-generic-password"]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    return security, tmp_path


def test_named_record_is_filled_and_the_password_stays_out_of_the_result(vault) -> None:
    security, tmp_path = vault
    record = add_record(SITE, LOGIN, SECRET, shared=False, bot_id=ANYA, run=security)
    page = FakePage(True)
    result = perform(URL, ANYA, "открой почту Аня", run=security, page=page)
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["login"] == "entered"
    assert result["text"] == "вошли "
    assert SECRET not in encoded
    assert "password" not in result
    assert "cookie" not in result
    assert (page.filled[0][1], page.filled[1][1]) == (LOGIN, SECRET)
    assert page.submitted is True
    finds = _finds(security)
    assert len(finds) == 1
    assert finds[0][finds[0].index("-s") + 1] == SERVICE_NAME
    assert finds[0][finds[0].index("-a") + 1] == record["id"]
    assert SECRET not in (tmp_path / "vault.json").read_text(encoding="utf-8")


def test_open_session_does_not_read_the_keychain(vault) -> None:
    security, _tmp_path = vault
    add_record(SITE, LOGIN, SECRET, shared=False, bot_id=ANYA, run=security)
    page = FakePage(False)
    result = perform(URL, ANYA, "открой почту Аня", run=security, page=page)
    assert result == {"text": PAGE, "login": "session"}
    assert _finds(security) == []
    assert page.filled == []
    assert page.submitted is False


def test_two_named_records_ask_without_reading_the_keychain(vault) -> None:
    security, _tmp_path = vault
    add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    add_record(SITE, OTHER, OTHER_SECRET, shared=False, bot_id=ANYA, run=security)
    page = FakePage(True)
    result = perform(URL, ANYA, "Аня или Борис", run=security, page=page)
    assert result["login"] == "choose"
    assert result["site"] == SITE
    assert result["choices"] == [LOGIN, OTHER]
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    assert OTHER_SECRET not in json.dumps(result, ensure_ascii=False)
    assert _finds(security) == []
    assert page.submitted is False


def test_unnamed_record_asks_then_the_chosen_login_is_filled(vault) -> None:
    security, _tmp_path = vault
    add_record(SITE, LOGIN, SECRET, shared=False, bot_id=ANYA, run=security)
    page = FakePage(True)
    asked = perform(URL, ANYA, "сценарий", run=security, page=page)
    assert asked["login"] == "choose"
    assert asked["choices"] == [LOGIN]
    assert _finds(security) == []
    assert page.submitted is False
    filled = perform(URL, ANYA, "сценарий", chosen=LOGIN, run=security, page=page)
    assert filled["login"] == "entered"
    assert SECRET not in json.dumps(filled, ensure_ascii=False)
    assert page.filled[-1][1] == SECRET
    assert len(_finds(security)) == 1
    ordinary = FakePage(True)
    skipped = perform(URL, ANYA, "сценарий", chosen="не тот", run=security, page=ordinary)
    assert skipped["login"] == "choose"
    assert ordinary.submitted is False
    assert len(_finds(security)) == 1


def test_other_bots_personal_record_is_not_entered(vault) -> None:
    security, _tmp_path = vault
    add_record(SITE, LOGIN, SECRET, shared=False, bot_id=BORIS, run=security)
    page = FakePage(True)
    result = perform(URL, ANYA, "открой почту Аня", run=security, page=page)
    assert result["login"] == "form"
    assert result["site"] == SITE
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    assert _finds(security) == []
    assert page.filled == []
    assert page.submitted is False


def test_no_record_returns_the_form_outcome_without_the_keychain(vault) -> None:
    security, _tmp_path = vault
    page = FakePage(True)
    result = perform(URL, ANYA, "открой почту Аня", run=security, page=page)
    assert result == {"text": PAGE, "login": "form", "site": SITE}
    assert security.commands == []
    assert page.submitted is False


def test_sign_in_route_returns_a_session_and_refuses_a_password(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    security, _tmp_path = vault
    opened = serve_password_screen(run=security)
    cabinet = "http://127.0.0.1:8765"
    opened.httpd.cabinet_origin = cabinet
    calls: list[str] = []

    def open_address(url: str) -> str:
        calls.append(url)
        return PAGE

    monkeypatch.setattr("desktop.browser.open_address", open_address)
    try:
        leaked = json.dumps(
            {
                "url": URL,
                "bot_id": ANYA,
                "scenario": "Аня",
                "password": SECRET,
                "cookie": "vault-cookie-9",
            }
        ).encode()
        status, body, _headers = _exchange(
            opened,
            "POST",
            "/sign-in",
            leaked,
            _json_headers(opened, cabinet, leaked),
        )
        assert status == 400
        assert calls == []
        assert SECRET.encode() not in body
        assert b"vault-cookie-9" not in body
        payload = json.dumps({"url": URL, "bot_id": ANYA, "scenario": "Аня"}).encode()
        opened_status, opened_body, opened_headers = _exchange(
            opened,
            "POST",
            "/sign-in",
            payload,
            _json_headers(opened, cabinet, payload),
        )
        assert opened_status == 200
        assert json.loads(opened_body.decode()) == {"text": PAGE, "login": "session"}
        assert calls == [URL]
        assert _header(opened_headers, "Access-Control-Allow-Origin") == cabinet
        assert SECRET.encode() not in opened_body
        foreign = json.dumps({"url": URL, "bot_id": ANYA, "scenario": "Аня"}).encode()
        foreign_status, foreign_body, _foreign_headers = _exchange(
            opened,
            "POST",
            "/sign-in",
            foreign,
            _json_headers(opened, "http://evil.example", foreign),
        )
        assert foreign_status == 403
        assert calls == [URL]
        assert SECRET.encode() not in foreign_body
        option_status, option_body, option_headers = _exchange(
            opened,
            "OPTIONS",
            "/sign-in",
            headers={"Origin": cabinet},
        )
        assert option_status == 204
        assert option_body == b""
        assert _header(option_headers, "Access-Control-Allow-Origin") == cabinet
        assert _finds(security) == []
    finally:
        opened.close()


def test_protected_form_defaults_to_personal_and_the_switch_shares(vault) -> None:
    security, tmp_path = vault
    opened = serve_password_screen(run=security)
    try:
        echoed, echoed_body, _echoed_headers = _exchange(
            opened,
            "GET",
            f"/?bot={ANYA}&site={SITE}&password={SECRET}",
        )
        assert echoed == 200
        form = echoed_body.decode()
        assert "Защищённая форма" in form
        assert "Сделать запись общей" in form
        assert 'name="request_site"' in form
        assert "readonly" in form
        assert f'value="{SITE}"' in form
        assert SECRET not in form
        assert "http:" not in form
        assert "https:" not in form
        assert "innerHTML" not in form
        personal = urlencode(
            {
                "request_site": SITE,
                "request_login": LOGIN,
                "request_password": SECRET,
                "request_bot": ANYA,
            }
        ).encode()
        saved, saved_body, saved_headers = _exchange(
            opened,
            "POST",
            "/vault/request",
            personal,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(personal)),
            },
        )
        assert saved == 303
        location = _header(saved_headers, "Location") or ""
        assert location == f"/?saved=1&bot={ANYA}&site={SITE}"
        assert SECRET not in location
        assert SECRET.encode() not in saved_body
        shown, shown_body, _shown_headers = _exchange(opened, "GET", location)
        assert shown == 200
        shown_text = shown_body.decode()
        assert "Пароль сохранён на этом компьютере." in shown_text
        assert SECRET not in shown_text
        assert 'type="password"' not in shown_text
        stored = records()
        assert stored[0]["shared"] is False
        assert stored[0]["bot_id"] == ANYA
        assert stored[0]["login"] == LOGIN
        assert read_record_password(str(stored[0]["id"]), ANYA, run=security) == SECRET
        shared = urlencode(
            {
                "request_site": SITE,
                "request_login": OTHER,
                "request_password": OTHER_SECRET,
                "request_bot": ANYA,
                "request_shared": "1",
            }
        ).encode()
        shared_status, shared_body, _shared_headers = _exchange(
            opened,
            "POST",
            "/vault/request",
            shared,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(shared)),
            },
        )
        assert shared_status == 303
        assert OTHER_SECRET.encode() not in shared_body
        shared_row = next(item for item in records() if item["login"] == OTHER)
        assert shared_row["shared"] is True
        assert shared_row["bot_id"] is None
        catalog = (tmp_path / "vault.json").read_text(encoding="utf-8")
        assert SECRET not in catalog
        assert OTHER_SECRET not in catalog
        blocked, blocked_body, _blocked_headers = _exchange(
            opened,
            "GET",
            f"/?bot={ANYA}&site={SITE}",
            headers={
                "Origin": "http://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        assert blocked == 403
        assert SECRET.encode() not in blocked_body
        same_site_fetch, same_site_body, _same_headers = _exchange(
            opened,
            "GET",
            f"/?bot={ANYA}&site={SITE}",
            headers={
                "Origin": "http://127.0.0.1:9",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        assert same_site_fetch == 403
        assert SECRET.encode() not in same_site_body
        opened_form, opened_body, _opened_headers = _exchange(
            opened,
            "GET",
            f"/?bot={ANYA}&site={SITE}",
            headers={
                "Origin": "http://127.0.0.1:9",
                "Referer": "http://127.0.0.1:9/chat",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        assert opened_form == 200
        opened_text = opened_body.decode()
        assert "Защищённая форма" in opened_text
        assert SECRET not in opened_text
        assert "vault-login" not in opened_text
        listed, listed_body, _listed_headers = _exchange(
            opened,
            "GET",
            "/",
            headers={
                "Origin": "http://127.0.0.1:9",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        assert listed == 403
        assert SECRET.encode() not in listed_body
        read, read_body, _read_headers = _exchange(
            opened,
            "GET",
            f"/passwords?site={quote(SITE)}&login={quote(LOGIN)}",
            headers={
                "Origin": "http://127.0.0.1:9",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        assert read == 403
        assert SECRET.encode() not in read_body
    finally:
        opened.close()


def test_protected_form_page_hides_the_password(vault) -> None:
    security, _tmp_path = vault
    opened = serve_password_screen(run=security)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.goto(f"{opened.url}/?bot={ANYA}&site={SITE}")
            assert "Защищённая форма" in page.locator("body").inner_text()
            assert page.locator('input[name="request_site"]').input_value() == SITE
            page.locator('input[name="request_login"]').fill(LOGIN)
            page.locator('input[name="request_password"]').fill(SECRET)
            page.get_by_role("button", name="Сохранить доступ", exact=True).click()
            page.wait_for_url(lambda url: "saved=1" in url)
            address = page.url
            content = page.content()
            visible = page.locator("body").inner_text()
            browser.close()
        assert SECRET not in content
        assert SECRET not in visible
        assert SECRET not in address
        assert "http:" not in content
        assert "https:" not in content
        stored = records()
        assert stored[0]["shared"] is False
        assert stored[0]["bot_id"] == ANYA
        assert read_record_password(str(stored[0]["id"]), ANYA, run=security) == SECRET
    finally:
        opened.close()


def _json_headers(opened, origin: str, body: bytes) -> dict[str, str]:
    return {
        "Origin": origin,
        "X-Desktop-Open": opened.open_token,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }


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
