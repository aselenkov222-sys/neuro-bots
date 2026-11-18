import json
import subprocess

SERVICE_NAME = "neuro-bots"
SECURITY_PATH = "/usr/bin/security"


class RecordRejected(Exception):
    pass


class KeychainError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def record_account(site: str, login: str) -> str:
    if site == "" or login == "":
        raise RecordRejected
    return json.dumps(
        {"login": login, "site": site},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def store_password(site: str, login: str, password: str, run=None) -> None:
    if password == "" or "\x00" in password:
        raise RecordRejected
    account = record_account(site, login)
    _security(
        [
            "add-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            account,
            "-w",
            password,
            "-U",
        ],
        run,
    )


def read_password(site: str, login: str, run=None) -> str | None:
    account = record_account(site, login)
    completed = _security(
        [
            "find-generic-password",
            "-s",
            SERVICE_NAME,
            "-a",
            account,
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
