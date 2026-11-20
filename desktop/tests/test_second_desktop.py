import json
import subprocess
from pathlib import Path

import pytest

from desktop.browser import browser_profile_dir
from desktop.identity import default_state_dir
from desktop.keychain import SECURITY_PATH, SERVICE_NAME
from desktop.vault import add_record, records

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "second-desktop-secret-7"
COOKIE = b"second-desktop-cookie-7"


class _Security:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        assert command[0] == SECURITY_PATH
        operation = command[1]
        service = command[command.index("-s") + 1]
        account = command[command.index("-a") + 1]
        assert service == SERVICE_NAME
        if operation == "add-generic-password":
            self.items[(service, account)] = command[command.index("-w") + 1]
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(operation)


def test_second_state_dir_does_not_receive_vault_cookies_or_browser_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    security = _Security()
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(old))
    assert default_state_dir() == old
    stored = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    profile = browser_profile_dir()
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(COOKIE)
    catalog = (old / "vault.json").read_text(encoding="utf-8")
    assert SECRET not in catalog
    assert json.loads(catalog)[0]["login"] == LOGIN
    assert stored["login"] == LOGIN
    assert SECRET not in json.dumps(stored)

    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(new))
    assert default_state_dir() == new
    assert records() == []
    assert not (new / "vault.json").exists()
    assert not browser_profile_dir().exists()
    assert SECRET.encode() not in _tree_bytes(new)
    assert COOKIE not in _tree_bytes(new)

    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(old))
    assert [item["login"] for item in records()] == [LOGIN]
    assert (old / "browser-profile" / "Cookies").read_bytes() == COOKIE
    assert SECRET.encode() not in (old / "vault.json").read_bytes()
    assert SECRET.encode() not in (old / "browser-profile" / "Cookies").read_bytes()


def _tree_bytes(root: Path) -> bytes:
    if not root.exists():
        return b""
    chunks: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"".join(chunks)
