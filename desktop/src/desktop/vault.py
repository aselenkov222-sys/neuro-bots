import json
import subprocess
import uuid
from pathlib import Path

from desktop.identity import default_state_dir
from desktop.keychain import SECURITY_PATH, SERVICE_NAME, KeychainError, RecordRejected

_CATALOG = "vault.json"


def add_record(
    site: str,
    login: str,
    password: str,
    *,
    shared: bool,
    bot_id: str | None = None,
    run=None,
) -> dict[str, str | bool | None]:
    _accept(site, login, password, shared, bot_id)
    catalog = _load()
    if any(_same_owner(record, site, login, shared, bot_id) for record in catalog):
        raise RecordRejected
    record_id = str(uuid.uuid4())
    _security(
        [
            "add-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            record_id,
            "-w",
            password,
            "-U",
        ],
        run,
    )
    catalog.append(_stored(record_id, site, login, shared, bot_id))
    _save(catalog)
    return _view(catalog[-1])


def records_for_bot(bot_id: str) -> list[dict[str, str | bool | None]]:
    return [_view(record) for record in _load() if _visible(record, bot_id)]


def read_record_password(record_id: str, bot_id: str, run=None) -> str | None:
    record = next((item for item in _load() if item["id"] == record_id), None)
    if record is None or not _visible(record, bot_id):
        return None
    completed = _security(
        [
            "find-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            record["id"],
            "-w",
        ],
        run,
        check=False,
    )
    if completed.returncode != 0:
        return None
    # `security -w` prints the password and then a newline.
    text = completed.stdout
    if text.endswith("\n"):
        text = text[:-1]
    return text


def _accept(
    site: str,
    login: str,
    password: str,
    shared: bool,
    bot_id: str | None,
) -> None:
    if site == "" or login == "" or password == "" or "\x00" in password:
        raise RecordRejected
    if shared is True:
        if bot_id is not None:
            raise RecordRejected
        return
    if shared is False and isinstance(bot_id, str) and bot_id != "":
        return
    raise RecordRejected


def _same_owner(
    record: dict,
    site: str,
    login: str,
    shared: bool,
    bot_id: str | None,
) -> bool:
    if record["site"] != site or record["login"] != login or record["shared"] is not shared:
        return False
    if shared:
        return True
    return record["bot_id"] == bot_id


def _visible(record: dict, bot_id: str) -> bool:
    if record["shared"]:
        return True
    return record["bot_id"] == bot_id


def _stored(
    record_id: str,
    site: str,
    login: str,
    shared: bool,
    bot_id: str | None,
) -> dict:
    record = {"id": record_id, "site": site, "login": login, "shared": shared}
    if not shared:
        record["bot_id"] = bot_id
    return record


def _view(record: dict) -> dict[str, str | bool | None]:
    return {
        "id": record["id"],
        "site": record["site"],
        "login": record["login"],
        "shared": record["shared"],
        "bot_id": None if record["shared"] else record["bot_id"],
    }


def _catalog_path() -> Path:
    return default_state_dir() / _CATALOG


def _load() -> list[dict]:
    path = _catalog_path()
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save(records: list[dict]) -> None:
    path = _catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _security(args: list[str], run, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [SECURITY_PATH, *args]
    if run is None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    else:
        completed = run(command)
    if check and completed.returncode != 0:
        raise KeychainError(completed.returncode)
    return completed
