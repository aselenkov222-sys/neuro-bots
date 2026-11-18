import json
import subprocess
import uuid
from pathlib import Path

from desktop.identity import default_state_dir
from desktop.keychain import SECURITY_PATH, SERVICE_NAME, KeychainError, RecordRejected

_CATALOG = "vault.json"
# `security` reports a missing item with status 44.
_ITEM_MISSING = 44


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
    _write_secret(record_id, password, run)
    catalog.append(_stored(record_id, site, login, shared, bot_id))
    _save(catalog)
    return _view(catalog[-1])


def user_add_from_profile(
    site: str,
    login: str,
    password: str,
    *,
    bot_id: str | None = None,
    run=None,
) -> dict[str, str | bool | None]:
    if bot_id is None:
        return add_record(site, login, password, shared=True, run=run)
    return add_record(site, login, password, shared=False, bot_id=bot_id, run=run)


def user_add_requested(
    site: str,
    login: str,
    password: str,
    bot_id: str,
    *,
    shared: bool = False,
    run=None,
) -> dict[str, str | bool | None]:
    if shared is True:
        return add_record(site, login, password, shared=True, run=run)
    if shared is False:
        return add_record(site, login, password, shared=False, bot_id=bot_id, run=run)
    raise RecordRejected


def user_replace(
    record_id: str,
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
    index = next((i for i, record in enumerate(catalog) if record["id"] == record_id), None)
    if index is None:
        raise RecordRejected
    if any(
        _same_owner(record, site, login, shared, bot_id)
        for record in catalog
        if record["id"] != record_id
    ):
        raise RecordRejected
    _write_secret(record_id, password, run)
    catalog[index] = _stored(record_id, site, login, shared, bot_id)
    _save(catalog)
    return _view(catalog[index])


def user_delete(record_id: str, run=None) -> None:
    catalog = _load()
    if not any(record["id"] == record_id for record in catalog):
        raise RecordRejected
    _delete_secret(record_id, run)
    _save([record for record in catalog if record["id"] != record_id])


def forget_bot(bot_id: str, run=None) -> None:
    personal_ids = [
        record["id"]
        for record in _load()
        if record["shared"] is False and record.get("bot_id") == bot_id
    ]
    for record_id in personal_ids:
        user_delete(record_id, run=run)


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


def _write_secret(record_id: str, password: str, run) -> None:
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


def _delete_secret(record_id: str, run) -> None:
    completed = _security(
        [
            "delete-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            record_id,
        ],
        run,
        check=False,
    )
    if completed.returncode in (0, _ITEM_MISSING):
        return
    raise KeychainError(completed.returncode)


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
