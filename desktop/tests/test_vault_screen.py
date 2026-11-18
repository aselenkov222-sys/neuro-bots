import http.client
import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest
from playwright.sync_api import sync_playwright

from desktop.keychain import SECURITY_PATH, SERVICE_NAME, record_account
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.vault import read_record_password, records

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"
OTHER_SECRET = "machine-secret-8"
ANYA = "bot-anya"
ANYA_NAME = "Искра"
_SAVED = "Пароль сохранён на этом компьютере."


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
            stored = self.items.get((service, account))
            if stored is None:
                return subprocess.CompletedProcess(command, 44, "", "")
            return subprocess.CompletedProcess(command, 0, stored + "\n", "")
        if operation == "delete-generic-password":
            if (service, account) not in self.items:
                return subprocess.CompletedProcess(command, 44, "", "")
            del self.items[(service, account)]
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(operation)


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    security = FakeSecurity()
    opened = serve_password_screen(run=security)
    try:
        yield opened, security, tmp_path
    finally:
        opened.close()


def test_profile_control_saves_a_shared_record_without_showing_the_password(screen) -> None:
    opened, security, tmp_path = screen
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.goto(opened.url + "/")
        page.locator('input[name="vault_site"]').fill(SITE)
        page.locator('input[name="vault_login"]').fill(LOGIN)
        page.locator('input[name="vault_password"]').fill(SECRET)
        page.get_by_role("button", name="Добавить запись").click()
        page.wait_for_url("**/?saved=1")
        content = page.content()
        visible = page.locator("body").inner_text()
        browser.close()
    assert _SAVED in visible
    assert SITE in content
    assert LOGIN in content
    assert 'class="vault-owner">общая<' in content
    assert SECRET not in content
    assert SECRET not in visible
    assert "proto-nav" not in content
    assert "http:" not in content
    assert "https:" not in content
    stored = records()
    assert len(stored) == 1
    assert stored[0]["shared"] is True
    assert stored[0]["bot_id"] is None
    assert stored[0]["site"] == SITE
    assert stored[0]["login"] == LOGIN
    assert security.commands[0][1] == "add-generic-password"
    account = security.commands[0][security.commands[0].index("-a") + 1]
    assert account == stored[0]["id"]
    assert account != record_account(SITE, LOGIN)
    catalog = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert SECRET not in catalog


def test_personal_record_shows_the_bot_name_and_not_the_password(screen) -> None:
    opened, _security, tmp_path = screen
    (tmp_path / "vault-bot-names.json").write_text(
        json.dumps({ANYA: ANYA_NAME}, ensure_ascii=False),
        encoding="utf-8",
    )
    status, body, _headers = _vault_add(
        opened,
        SITE,
        LOGIN,
        SECRET,
        personal=True,
        bot_id=ANYA,
    )
    assert status == 303
    assert SECRET.encode() not in body
    page_status, page, _page_headers = _exchange(opened, "GET", "/?saved=1")
    assert page_status == 200
    text = page.decode()
    assert f'class="vault-owner">{ANYA_NAME}<' in text
    assert SITE in text
    assert LOGIN in text
    assert SECRET not in text
    assert SECRET not in (tmp_path / "vault-bot-names.json").read_text(encoding="utf-8")
    stored = records()
    assert stored[0]["shared"] is False
    assert stored[0]["bot_id"] == ANYA


def test_cabinet_bot_names_appear_in_the_list_and_the_select(screen) -> None:
    opened, _security, tmp_path = screen
    status, body, _headers = _vault_add(
        opened,
        SITE,
        LOGIN,
        SECRET,
        personal=True,
        bot_id=ANYA,
    )
    assert status == 303
    assert SECRET.encode() not in body
    cabinet = "http://127.0.0.1:8765"
    opened.httpd.cabinet_origin = cabinet
    names_path = tmp_path / "vault-bot-names.json"
    payload = json.dumps(
        {"bots": [{"id": ANYA, "name": ANYA_NAME}]},
        ensure_ascii=False,
    ).encode()
    leaked = json.dumps(
        {
            "bots": [{"id": ANYA, "name": ANYA_NAME}],
            "password": SECRET,
            "login": LOGIN,
            "site": SITE,
        },
        ensure_ascii=False,
    ).encode()
    foreign, foreign_body, _foreign_headers = _exchange(
        opened,
        "POST",
        "/bot-names",
        leaked,
        _json_headers(leaked, "http://evil.example", opened.open_token),
    )
    assert foreign == 403
    assert SECRET.encode() not in foreign_body
    assert not names_path.exists()
    options, options_body, options_headers = _exchange(
        opened,
        "OPTIONS",
        "/bot-names",
        headers={
            "Origin": cabinet,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-desktop-open",
        },
    )
    assert options == 204
    assert SECRET.encode() not in options_body
    assert _header(options_headers, "Access-Control-Allow-Origin") == cabinet
    assert "X-Desktop-Open" in (_header(options_headers, "Access-Control-Allow-Headers") or "")
    saved, saved_body, saved_headers = _exchange(
        opened,
        "POST",
        "/bot-names",
        payload,
        _json_headers(payload, cabinet, opened.open_token),
    )
    assert saved == 204
    assert SECRET.encode() not in saved_body
    assert _header(saved_headers, "Access-Control-Allow-Origin") == cabinet
    written = names_path.read_text(encoding="utf-8")
    assert json.loads(written) == {ANYA: ANYA_NAME}
    assert SECRET not in written
    refused, refused_body, _refused_headers = _exchange(
        opened,
        "POST",
        "/bot-names",
        leaked,
        _json_headers(leaked, cabinet, opened.open_token),
    )
    assert refused == 400
    assert SECRET.encode() not in refused_body
    nested = json.dumps(
        {"bots": [{"id": ANYA, "name": ANYA_NAME, "password": SECRET}]},
        ensure_ascii=False,
    ).encode()
    nested_status, nested_body, _nested_headers = _exchange(
        opened,
        "POST",
        "/bot-names",
        nested,
        _json_headers(nested, cabinet, opened.open_token),
    )
    assert nested_status == 400
    assert SECRET.encode() not in nested_body
    assert names_path.read_text(encoding="utf-8") == written
    page_status, page, _page_headers = _exchange(opened, "GET", "/")
    assert page_status == 200
    text = page.decode()
    assert f'class="vault-owner">{ANYA_NAME}<' in text
    assert f'class="vault-owner">{ANYA}<' not in text
    add_form = text.split('action="/vault"', 1)[1].split("</form>", 1)[0]
    assert '<select name="vault_bot"><option value="" selected>общая</option>' in add_form
    assert f'<option value="{ANYA}">{ANYA_NAME}</option>' in add_form
    assert f'<option value="{ANYA}" selected>' not in add_form
    assert f'<option value="{ANYA}" selected>{ANYA_NAME}</option>' in text
    assert '<input name="vault_bot"' not in text
    assert SECRET not in text
    chosen = urlencode(
        {
            "vault_site": "другой.example",
            "vault_login": "Борис",
            "vault_password": OTHER_SECRET,
            "vault_bot": ANYA,
        }
    ).encode()
    chosen_status, chosen_body, _chosen_headers = _exchange(
        opened,
        "POST",
        "/vault",
        chosen,
        _form_headers(chosen),
    )
    assert chosen_status == 303
    assert SECRET.encode() not in chosen_body
    assert OTHER_SECRET.encode() not in chosen_body
    personal = [record for record in records() if record["login"] == "Борис"]
    assert len(personal) == 1
    assert personal[0]["shared"] is False
    assert personal[0]["bot_id"] == ANYA
    follow_status, follow, _follow_headers = _exchange(opened, "GET", "/?saved=1")
    assert follow_status == 200
    follow_text = follow.decode()
    assert follow_text.count(f'class="vault-owner">{ANYA_NAME}<') == 2
    assert SECRET not in follow_text
    assert OTHER_SECRET not in follow_text
    assert SECRET not in names_path.read_text(encoding="utf-8")
    assert OTHER_SECRET not in names_path.read_text(encoding="utf-8")


def test_missing_bot_name_shows_the_bot_id(screen) -> None:
    opened, _security, _tmp_path = screen
    status, _body, _headers = _vault_add(
        opened,
        SITE,
        LOGIN,
        SECRET,
        personal=True,
        bot_id=ANYA,
    )
    assert status == 303
    _page_status, page, _page_headers = _exchange(opened, "GET", "/")
    text = page.decode()
    assert f'class="vault-owner">{ANYA}<' in text
    assert SECRET not in text


def test_copy_control_does_not_place_the_password_in_the_page(screen) -> None:
    opened, security, _tmp_path = screen
    status, body, _headers = _vault_add(opened, SITE, LOGIN, SECRET)
    assert status == 303
    assert SECRET.encode() not in body
    record_id = records()[0]["id"]
    before = len(security.commands)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.goto(opened.url + "/")
        assert SECRET not in page.content()
        page.locator("[data-vault-copy]").click()
        page.locator("#vault-copy-status").wait_for(state="visible")
        content = page.content()
        visible = page.locator("body").inner_text()
        browser.close()
    assert SECRET not in content
    assert SECRET not in visible
    assert (
        "Пароль скопирован." in visible
        or "Пароль остаётся на этом компьютере." in visible
    )
    assert security.commands[before][1] == "find-generic-password"
    assert security.commands[before][security.commands[before].index("-a") + 1] == record_id
    assert "add-generic-password" not in {command[1] for command in security.commands[before:]}
    assert "delete-generic-password" not in {command[1] for command in security.commands[before:]}


def test_cabinet_origin_is_refused_for_vault_routes(screen) -> None:
    opened, security, _tmp_path = screen
    cabinet = {"Origin": "http://127.0.0.1:9", "Sec-Fetch-Site": "same-site"}
    refused, refused_body, _headers = _vault_add(
        opened,
        SITE,
        LOGIN,
        SECRET,
        headers=cabinet,
    )
    assert refused == 403
    assert SECRET.encode() not in refused_body
    assert records() == []
    assert security.commands == []
    saved, saved_body, _saved_headers = _vault_add(opened, SITE, LOGIN, SECRET)
    assert saved == 303
    assert SECRET.encode() not in saved_body
    record_id = records()[0]["id"]
    read_refused, read_body, _read_headers = _exchange(
        opened,
        "GET",
        f"/vault/{record_id}",
        headers=cabinet,
    )
    assert read_refused == 403
    assert SECRET.encode() not in read_body
    replace_body = urlencode(
        {"vault_site": SITE, "vault_login": "Борис", "vault_password": OTHER_SECRET}
    ).encode()
    replace_refused, replace_refused_body, _replace_headers = _exchange(
        opened,
        "POST",
        f"/vault/{record_id}",
        replace_body,
        _form_headers(replace_body, cabinet),
    )
    assert replace_refused == 403
    assert OTHER_SECRET.encode() not in replace_refused_body
    assert SECRET.encode() not in replace_refused_body
    delete_refused, delete_body, _delete_headers = _exchange(
        opened,
        "POST",
        f"/vault/{record_id}/delete",
        b"",
        _form_headers(b"", cabinet),
    )
    assert delete_refused == 403
    assert SECRET.encode() not in delete_body
    assert records()[0]["login"] == LOGIN
    assert read_record_password(record_id, "screen", run=security) == SECRET


def test_replace_and_delete_do_not_show_the_password(screen) -> None:
    opened, security, tmp_path = screen
    _vault_add(opened, SITE, LOGIN, SECRET)
    record_id = records()[0]["id"]
    replace_body = urlencode(
        {"vault_site": SITE, "vault_login": "Борис", "vault_password": OTHER_SECRET}
    ).encode()
    status, body, _headers = _exchange(
        opened,
        "POST",
        f"/vault/{record_id}",
        replace_body,
        _form_headers(replace_body),
    )
    assert status == 303
    assert SECRET.encode() not in body
    assert OTHER_SECRET.encode() not in body
    page_status, page, _page_headers = _exchange(opened, "GET", "/?saved=1")
    assert page_status == 200
    text = page.decode()
    assert "Борис" in text
    assert SECRET not in text
    assert OTHER_SECRET not in text
    assert read_record_password(record_id, "screen", run=security) == OTHER_SECRET
    catalog = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert SECRET not in catalog
    assert OTHER_SECRET not in catalog
    deleted, deleted_body, _deleted_headers = _exchange(
        opened,
        "POST",
        f"/vault/{record_id}/delete",
        b"",
        _form_headers(b""),
    )
    assert deleted == 303
    assert OTHER_SECRET.encode() not in deleted_body
    assert records() == []
    assert (SERVICE_NAME, record_id) not in security.items
    assert security.commands[-1][1] == "delete-generic-password"
    empty_status, empty_page, _empty_headers = _exchange(opened, "GET", "/")
    assert empty_status == 200
    assert OTHER_SECRET.encode() not in empty_page


def test_old_password_post_does_not_create_a_vault_record(screen) -> None:
    opened, security, _tmp_path = screen
    status, body, headers = _post_password(opened, SITE, LOGIN, SECRET)
    assert status == 303
    assert _header(headers, "Location") == "/?saved=1"
    assert SECRET.encode() not in body
    assert records() == []
    assert [command[1] for command in security.commands] == ["add-generic-password"]
    page_status, page, _page_headers = _exchange(opened, "GET", "/?saved=1")
    assert page_status == 200
    text = page.decode()
    assert 'action="/passwords"' in text
    assert 'fetch("/passwords?"' in text
    assert _SAVED in text
    assert SECRET not in text
    assert 'class="vault-owner"' not in text


def _vault_add(opened, site, login, password, personal=False, bot_id="", headers=None):
    fields = {"vault_site": site, "vault_login": login, "vault_password": password}
    if personal:
        fields["vault_personal"] = "1"
        fields["vault_bot"] = bot_id
    body = urlencode(fields).encode()
    return _exchange(opened, "POST", "/vault", body, _form_headers(body, headers))


def _post_password(opened, site, login, password):
    body = urlencode({"site": site, "login": login, "password": password}).encode()
    return _exchange(opened, "POST", "/passwords", body, _form_headers(body))


def _json_headers(body: bytes, origin: str, token: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "X-Desktop-Open": token,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }


def _form_headers(body: bytes, extra: dict[str, str] | None = None) -> dict[str, str]:
    sent = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    }
    if extra:
        sent.update(extra)
    return sent


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
