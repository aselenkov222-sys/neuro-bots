import http.client
import json
import subprocess
from pathlib import Path

import pytest

import desktop.vault as vault_module
from desktop.browser import browser_profile_dir
from desktop.keychain import SECURITY_PATH
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.removal import MARKER_NAME, mark_account_removed, prepare_launch
from desktop.vault import add_record, read_record_password
from desktop.window import main

WEB_APP = Path(__file__).resolve().parents[2] / "web" / "app.js"
SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"


class FakeSecurity:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
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
        if operation == "delete-generic-password":
            if (service, account) not in self.items:
                return subprocess.CompletedProcess(command, 44, "", "")
            del self.items[(service, account)]
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(operation)


def _forbid_real_security(*args, **kwargs):
    raise AssertionError("real security was called")


def _post(opened, payload: dict, origin: str) -> tuple[int, bytes]:
    body = json.dumps(payload).encode()
    connection = http.client.HTTPConnection(LOOPBACK, opened.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/account-removed",
            body=body,
            headers={
                "Origin": origin,
                "X-Desktop-Open": opened.open_token,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_next_launch_drops_the_local_password_and_browser_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    record = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    profile = browser_profile_dir()
    profile.mkdir()
    (profile / "Cookies").write_text("session-cookie", encoding="utf-8")
    (tmp_path / "desktop-id").write_text("kept", encoding="utf-8")
    mark_account_removed()
    assert read_record_password(record["id"], "bot-anya", run=security) == SECRET
    assert profile.is_dir()
    prepare_launch(run=security)
    assert read_record_password(record["id"], "bot-anya", run=security) is None
    assert SECRET not in security.items.values()
    assert not profile.exists()
    assert not (tmp_path / MARKER_NAME).exists()
    assert (tmp_path / "desktop-id").read_text(encoding="utf-8") == "kept"
    assert list(tmp_path.iterdir()) == [tmp_path / "desktop-id"]
    prepare_launch(run=security)
    assert list(tmp_path.iterdir()) == [tmp_path / "desktop-id"]


def test_launch_keeps_the_vault_when_the_account_stays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    record = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    profile = browser_profile_dir()
    profile.mkdir()
    (profile / "Cookies").write_text("session-cookie", encoding="utf-8")
    prepare_launch(run=security)
    assert read_record_password(record["id"], "bot-anya", run=security) == SECRET
    assert (profile / "Cookies").read_text(encoding="utf-8") == "session-cookie"
    assert SECRET in security.items.values()


def test_deletion_marks_the_next_launch_and_refuses_a_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    record = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    profile = browser_profile_dir()
    profile.mkdir()
    opened = serve_password_screen()
    try:
        cabinet = "http://127.0.0.1:8765"
        opened.httpd.cabinet_origin = cabinet
        refused, refused_body = _post(
            opened,
            {"password": SECRET, "cookie": "vault-cookie-9"},
            cabinet,
        )
        assert refused == 400
        assert SECRET.encode() not in refused_body
        assert b"vault-cookie-9" not in refused_body
        assert not (tmp_path / MARKER_NAME).exists()
        assert read_record_password(record["id"], "bot-anya", run=security) == SECRET
        status, body = _post(opened, {}, cabinet)
        assert status == 204
        assert body == b""
        assert (tmp_path / MARKER_NAME).is_file()
        assert profile.is_dir()
        assert read_record_password(record["id"], "bot-anya", run=security) == SECRET
    finally:
        opened.close()
    prepare_launch(run=security)
    assert not profile.exists()
    assert read_record_password(record["id"], "bot-anya", run=security) is None


def test_main_prepares_the_launch_before_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        "desktop.removal.prepare_launch",
        lambda run=None: order.append("prepare"),
    )
    monkeypatch.setattr(
        "desktop.password_screen.start_password_screen",
        lambda: order.append("screen"),
    )
    monkeypatch.setattr(
        "desktop.window.open_cabinet",
        lambda origin, on_ready=None: order.append(origin),
    )
    main(["http://cabinet.example"])
    assert order == ["prepare", "screen", "http://cabinet.example"]


def test_only_account_removal_asks_the_desktop_to_forget_local_state() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    removal = source[
        source.index("async function noteAccountRemoved()") : source.index(
            "async function signOut()"
        )
    ]
    sign_out = source[
        source.index("async function signOut()") : source.index("async function boot()")
    ]
    assert "/account-removed" in removal
    assert "password" not in removal
    assert "cookie" not in removal
    assert "/account-removed" not in sign_out
