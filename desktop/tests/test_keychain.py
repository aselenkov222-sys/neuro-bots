import json
import subprocess
from pathlib import Path

import pytest

from desktop.keychain import (
    SECURITY_PATH,
    SERVICE_NAME,
    KeychainError,
    RecordRejected,
    read_password,
    record_account,
    store_password,
)

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"
OTHER_SECRET = "machine-secret-8"


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


def test_store_writes_the_macos_keychain_item() -> None:
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    assert security.commands == [
        [
            SECURITY_PATH,
            "add-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            record_account(SITE, LOGIN),
            "-w",
            SECRET,
            "-U",
        ]
    ]
    assert SECRET not in record_account(SITE, LOGIN)


def test_read_returns_the_keychain_password() -> None:
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    assert read_password(SITE, LOGIN, run=security) == SECRET
    assert security.commands[-1] == [
        SECURITY_PATH,
        "find-generic-password",
        "-s",
        SERVICE_NAME,
        "-a",
        record_account(SITE, LOGIN),
        "-w",
    ]


def test_same_site_keeps_a_separate_password_per_login() -> None:
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    store_password(SITE, "Борис", OTHER_SECRET, run=security)
    assert read_password(SITE, LOGIN, run=security) == SECRET
    assert read_password(SITE, "Борис", run=security) == OTHER_SECRET


def test_saving_again_replaces_the_keychain_password() -> None:
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    store_password(SITE, LOGIN, OTHER_SECRET, run=security)
    assert read_password(SITE, LOGIN, run=security) == OTHER_SECRET
    assert len(security.items) == 1


def test_missing_item_reads_as_absent() -> None:
    security = FakeSecurity()
    assert read_password(SITE, LOGIN, run=security) is None


def test_empty_record_does_not_call_keychain() -> None:
    security = FakeSecurity()
    with pytest.raises(RecordRejected):
        store_password("", LOGIN, SECRET, run=security)
    with pytest.raises(RecordRejected):
        store_password(SITE, "", SECRET, run=security)
    with pytest.raises(RecordRejected):
        store_password(SITE, LOGIN, "", run=security)
    with pytest.raises(RecordRejected):
        store_password(SITE, LOGIN, "a\x00b", run=security)
    assert security.commands == []


def test_failed_write_does_not_report_the_password() -> None:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "denied")

    with pytest.raises(KeychainError) as caught:
        store_password(SITE, LOGIN, SECRET, run=run)
    assert SECRET not in str(caught.value)
    assert caught.value.status == 1


def test_password_is_not_written_into_the_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    assert read_password(SITE, LOGIN, run=security) == SECRET
    written = [path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()]
    assert SECRET not in "\n".join(written)
    assert json.loads(record_account(SITE, LOGIN)) == {"login": LOGIN, "site": SITE}
