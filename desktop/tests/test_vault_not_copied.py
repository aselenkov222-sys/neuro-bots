import subprocess
from pathlib import Path

import pytest

import desktop.vault as vault_module
from desktop.keychain import SECURITY_PATH, SERVICE_NAME
from desktop.vault import add_record, read_record_password, records

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"
BOT = "bot-anya"


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


def _forbid_real_security(*args, **kwargs):
    raise AssertionError("real security was called")


def _files(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.is_file()]


def test_a_record_stays_in_the_desktop_that_saved_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    first = tmp_path / "desktop-a"
    second = tmp_path / "desktop-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(first))
    saved = add_record(SITE, LOGIN, SECRET, shared=False, bot_id=BOT, run=security)
    found = records()
    assert [item["id"] for item in found] == [saved["id"]]
    assert found[0]["site"] == SITE
    assert found[0]["login"] == LOGIN
    assert read_record_password(str(saved["id"]), BOT, run=security) == SECRET
    finds = [command for command in security.commands if command[1] == "find-generic-password"]
    assert finds[-1][finds[-1].index("-s") + 1] == SERVICE_NAME
    assert finds[-1][finds[-1].index("-a") + 1] == saved["id"]
    catalog = (first / "vault.json").read_text(encoding="utf-8")
    assert SECRET not in catalog
    assert saved["id"] in catalog
    assert not (second / "vault.json").exists()
    assert _files(second) == []
    reads_before = len(security.commands)
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(second))
    assert records() == []
    assert read_record_password(str(saved["id"]), BOT, run=security) is None
    assert security.commands[reads_before:] == []
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(first))
    assert [item["id"] for item in records()] == [saved["id"]]
    assert read_record_password(str(saved["id"]), BOT, run=security) == SECRET


def test_vault_modules_do_not_name_a_server_client() -> None:
    root = Path(vault_module.__file__).parent
    combined = (root / "vault.py").read_text(encoding="utf-8") + (
        root / "password_screen.py"
    ).read_text(encoding="utf-8")
    for name in ("psycopg", "sqlalchemy", "httpx", "urllib.request"):
        assert name not in combined
