import json
import subprocess
from pathlib import Path

import pytest

import desktop.vault as vault_module
from desktop.keychain import (
    SECURITY_PATH,
    SERVICE_NAME,
    KeychainError,
    RecordRejected,
    read_password,
    record_account,
    store_password,
)
from desktop.vault import add_record, read_record_password, records_for_bot

SITE = "почта.example"
LOGIN = "Аня"
SECRET = "machine-secret-7"
OTHER_SECRET = "machine-secret-8"
THIRD_SECRET = "machine-secret-9"
ANYA = "bot-anya"
BORIS = "bot-boris"


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


def _ids(items: list[dict]) -> set[str]:
    return {item["id"] for item in items}


def test_one_site_keeps_a_separate_password_per_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    first = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    second = add_record(SITE, "Борис", OTHER_SECRET, shared=True, run=security)
    assert first["id"] != second["id"]
    assert first["id"] != record_account(SITE, LOGIN)
    assert "password" not in first
    assert security.commands[0] == [
        SECURITY_PATH,
        "add-generic-password",
        "-s",
        SERVICE_NAME,
        "-a",
        first["id"],
        "-w",
        SECRET,
        "-U",
    ]
    assert read_record_password(first["id"], ANYA, run=security) == SECRET
    assert read_record_password(second["id"], BORIS, run=security) == OTHER_SECRET
    assert security.commands[-1] == [
        SECURITY_PATH,
        "find-generic-password",
        "-s",
        SERVICE_NAME,
        "-a",
        second["id"],
        "-w",
    ]


def test_shared_record_is_visible_to_two_bots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    shared = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    personal = add_record(
        SITE, "Борис", OTHER_SECRET, shared=False, bot_id=ANYA, run=security
    )
    taken = len(security.commands)
    anya_records = records_for_bot(ANYA)
    boris_records = records_for_bot(BORIS)
    assert len(security.commands) == taken
    assert _ids(anya_records) == {shared["id"], personal["id"]}
    assert _ids(boris_records) == {shared["id"]}
    shared_item = boris_records[0]
    assert shared_item["shared"] is True
    assert shared_item["bot_id"] is None
    personal_item = next(item for item in anya_records if item["id"] == personal["id"])
    assert personal_item["bot_id"] == ANYA
    for item in anya_records:
        assert set(item) == {"id", "site", "login", "shared", "bot_id"}
        assert "password" not in item
    assert SECRET not in json.dumps(anya_records)
    assert read_record_password(shared["id"], ANYA, run=security) == SECRET
    assert read_record_password(shared["id"], BORIS, run=security) == SECRET


def test_personal_record_is_visible_only_to_its_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    personal = add_record(SITE, LOGIN, SECRET, shared=False, bot_id=ANYA, run=security)
    assert personal["bot_id"] == ANYA
    assert personal["shared"] is False
    assert _ids(records_for_bot(ANYA)) == {personal["id"]}
    assert records_for_bot(BORIS) == []
    assert read_record_password(personal["id"], ANYA, run=security) == SECRET


def test_other_bot_does_not_read_a_personal_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    personal = add_record(SITE, LOGIN, SECRET, shared=False, bot_id=ANYA, run=security)
    taken = len(security.commands)
    assert read_record_password(personal["id"], BORIS, run=security) is None
    assert records_for_bot(BORIS) == []
    assert len(security.commands) == taken


def test_password_is_absent_from_the_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    shared = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    personal = add_record(
        SITE, "Борис", OTHER_SECRET, shared=False, bot_id=ANYA, run=security
    )
    assert read_record_password(shared["id"], ANYA, run=security) == SECRET
    assert read_record_password(personal["id"], ANYA, run=security) == OTHER_SECRET
    written = [
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()
    ]
    text = "\n".join(written)
    assert SECRET not in text
    assert OTHER_SECRET not in text
    stored = json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))
    assert stored[0]["shared"] is True
    assert "bot_id" not in stored[0]
    assert stored[1] == {
        "id": personal["id"],
        "site": SITE,
        "login": "Борис",
        "shared": False,
        "bot_id": ANYA,
    }
    for item in stored:
        assert "password" not in item
        assert set(item) <= {"id", "site", "login", "shared", "bot_id"}


def test_vault_source_does_not_name_a_server_client() -> None:
    source = Path(vault_module.__file__).read_text(encoding="utf-8")
    for name in ("psycopg", "sqlalchemy", "httpx", "urllib.request"):
        assert name not in source


def test_empty_record_does_not_call_security(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    with pytest.raises(RecordRejected):
        add_record("", LOGIN, SECRET, shared=True, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, "", SECRET, shared=True, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, "", shared=True, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, "a\x00b", shared=True, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, SECRET, shared=True, bot_id=ANYA, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, SECRET, shared=False, run=security)
    assert security.commands == []
    assert list(tmp_path.rglob("*")) == []


def test_same_owner_is_rejected_without_calling_security(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    shared = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    personal = add_record(SITE, LOGIN, OTHER_SECRET, shared=False, bot_id=ANYA, run=security)
    taken = len(security.commands)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, THIRD_SECRET, shared=True, run=security)
    with pytest.raises(RecordRejected):
        add_record(SITE, LOGIN, THIRD_SECRET, shared=False, bot_id=ANYA, run=security)
    assert len(security.commands) == taken
    assert THIRD_SECRET not in security.items.values()
    written = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()
    )
    assert THIRD_SECRET not in written
    assert read_record_password(shared["id"], BORIS, run=security) == SECRET
    assert read_record_password(personal["id"], ANYA, run=security) == OTHER_SECRET


def test_same_site_and_login_can_belong_to_the_account_and_each_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    shared = add_record(SITE, LOGIN, SECRET, shared=True, run=security)
    anya = add_record(SITE, LOGIN, OTHER_SECRET, shared=False, bot_id=ANYA, run=security)
    boris = add_record(SITE, LOGIN, THIRD_SECRET, shared=False, bot_id=BORIS, run=security)
    assert read_record_password(shared["id"], ANYA, run=security) == SECRET
    assert read_record_password(anya["id"], ANYA, run=security) == OTHER_SECRET
    assert read_record_password(boris["id"], BORIS, run=security) == THIRD_SECRET
    assert _ids(records_for_bot(ANYA)) == {shared["id"], anya["id"]}
    assert _ids(records_for_bot(BORIS)) == {shared["id"], boris["id"]}


def test_missing_record_does_not_call_security(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    security = FakeSecurity()
    assert read_record_password("missing", ANYA, run=security) is None
    assert security.commands == []


def test_failed_write_does_not_report_the_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "denied")

    with pytest.raises(KeychainError) as caught:
        add_record(SITE, LOGIN, SECRET, shared=True, run=run)
    assert SECRET not in str(caught.value)
    assert caught.value.status == 1
    assert list(tmp_path.rglob("*")) == []


def test_vault_item_does_not_collide_with_the_password_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(vault_module.subprocess, "run", _forbid_real_security)
    monkeypatch.setattr("desktop.keychain.subprocess.run", _forbid_real_security)
    security = FakeSecurity()
    store_password(SITE, LOGIN, SECRET, run=security)
    record = add_record(SITE, LOGIN, OTHER_SECRET, shared=True, run=security)
    assert record["id"] != record_account(SITE, LOGIN)
    assert read_password(SITE, LOGIN, run=security) == SECRET
    assert read_record_password(record["id"], ANYA, run=security) == OTHER_SECRET
