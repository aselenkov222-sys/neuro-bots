import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from configparser import ConfigParser
from pathlib import Path

import pytest

from server.aggregator import complete
from server.config import configured_aggregator_key
from server.db import DATABASE_URL, alembic_sqlalchemy_url, create_db_engine, database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO = PROJECT_ROOT.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
PASSWORD = "abcdef1!"
STUB = "Сейчас я на заглушке агрегатора и не вызываю модель."
LOCAL_DATABASE_URL = "postgresql+psycopg://neurobots@127.0.0.1:5432/neurobots"
MISSING_DATABASE_URL = "postgresql+psycopg://neurobots@127.0.0.1:5432/neurobots_missing"


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(PROJECT_ROOT),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _open(request: urllib.request.Request) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            header = response.headers.get("set-cookie", "")
            return response.status, response.read(), header
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("set-cookie", "")


def _wait_for_http(url: str) -> tuple[int, bytes]:
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, body, _header = _open(urllib.request.Request(url))
            return status, body
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = str(error)
            time.sleep(0.1)
    raise AssertionError(last_error)


def _stop(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
    try:
        stdout, _stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, _stderr = process.communicate(timeout=5)
    return stdout or ""


def _cookie_flags(header: str) -> tuple[str, list[str]]:
    parts = [part.strip() for part in header.split(";")]
    name, value = parts[0].split("=", 1)
    assert name == "session"
    return value, [part.lower() for part in parts[1:]]


def _server(port: int, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stand_env(database: str, sink: Path, key: str) -> dict[str, str]:
    env = os.environ.copy()
    env["NEURO_BOTS_STAND"] = "1"
    env["NEURO_BOTS_DATABASE_URL"] = database
    env["NEURO_BOTS_MAIL_SINK"] = str(sink)
    env["NEURO_BOTS_AGGREGATOR_KEY"] = key
    return env


def test_stand_serves_cabinet_and_json_on_one_origin_with_secure_session() -> None:
    key = "stand-key-" + secrets.token_hex(8)
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[str] | None = None
    session_id = ""
    journal = ""
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout
        upgrade_env = os.environ.copy()
        upgrade_env.pop("NEURO_BOTS_DATABASE_URL", None)
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            env=upgrade_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout

        with tempfile.TemporaryDirectory() as temporary:
            sink = Path(temporary) / "mail"
            missing_port = _free_port()
            missing_origin = f"http://127.0.0.1:{missing_port}"
            missing_server = _server(
                missing_port,
                _stand_env(MISSING_DATABASE_URL, sink, key),
            )
            try:
                missing_status, _missing_body = _wait_for_http(
                    f"{missing_origin}/health"
                )
                assert missing_status == 503
            finally:
                missing_journal = _stop(missing_server)
            assert key not in missing_journal
            assert PASSWORD not in missing_journal

            process = _server(port, _stand_env(LOCAL_DATABASE_URL, sink, key))
            status, body = _wait_for_http(f"{origin}/health")
            assert status == 200
            assert json.loads(body) == {"status": "ok"}

            cabinet_status, cabinet_body, cabinet_cookie = _open(
                urllib.request.Request(f"{origin}/")
            )
            assert cabinet_status == 200
            assert b'id="app"' in cabinet_body
            assert cabinet_cookie == ""
            assert key.encode() not in cabinet_body

            me_status, me_body, _me_cookie = _open(
                urllib.request.Request(f"{origin}/api/me")
            )
            assert me_status == 401
            assert json.loads(me_body)["code"] == "unauthenticated"
            assert key.encode() not in me_body

            email = "stand-session@example.com"
            register_status, register_body, register_cookie = _open(
                urllib.request.Request(
                    f"{origin}/api/register",
                    data=json.dumps({"email": email, "password": PASSWORD}).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin},
                    method="POST",
                )
            )
            assert register_status == 200
            registered = json.loads(register_body)
            assert registered["account"]["email"] == email
            session_id, flags = _cookie_flags(register_cookie)
            assert session_id == str(uuid.UUID(session_id))
            assert "httponly" in flags
            assert "samesite=lax" in flags
            assert "path=/" in flags
            assert "secure" in flags
            assert PASSWORD not in register_body.decode()
            assert PASSWORD not in register_cookie
            assert key.encode() not in register_body
            assert key not in register_cookie

            letter = sink.read_text(encoding="utf-8")
            assert email in letter
            assert f"{origin}/api/confirm-email?token=" in letter
            assert PASSWORD not in letter
            assert key not in letter

            logout_status, logout_body, logout_cookie = _open(
                urllib.request.Request(
                    f"{origin}/api/logout",
                    data=b"",
                    headers={"Origin": origin, "Cookie": f"session={session_id}"},
                    method="POST",
                )
            )
            assert logout_status == 200
            assert json.loads(logout_body) == {}
            _cleared, cleared_flags = _cookie_flags(logout_cookie)
            assert "secure" in cleared_flags
            assert "httponly" in cleared_flags
            assert "samesite=lax" in cleared_flags
            assert "max-age=0" in cleared_flags
            assert key not in logout_cookie
    finally:
        if process is not None:
            journal = _stop(process)
        _compose("down", "--remove-orphans", timeout=60)
    assert PASSWORD not in journal
    assert key not in journal
    assert session_id not in journal


def test_stand_database_url_and_aggregator_key_stay_out_of_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "a%b-" + secrets.token_hex(8)
    key = "stand-key-" + secrets.token_hex(8)
    url = (
        "postgresql+psycopg://neurobots:"
        + password.replace("%", "%25")
        + "@127.0.0.1:5432/neurobots"
    )
    monkeypatch.setenv("NEURO_BOTS_DATABASE_URL", url)
    monkeypatch.setenv("NEURO_BOTS_AGGREGATOR_KEY", key)
    assert database_url() == url
    parser = ConfigParser()
    parser.read_dict({"alembic": {"sqlalchemy.url": alembic_sqlalchemy_url()}})
    assert parser["alembic"]["sqlalchemy.url"] == url
    engine = create_db_engine()
    try:
        assert engine.url.password == password
    finally:
        engine.dispose()
    assert configured_aggregator_key() == key
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    names = [name for name in listed.stdout.split(b"\0") if name]
    assert all(name != b".env" and not name.endswith(b"/.env") for name in names)
    for name in names:
        blob = (REPO / os.fsdecode(name)).read_bytes()
        assert password.encode() not in blob
        assert key.encode() not in blob


def test_development_database_url_has_no_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEURO_BOTS_DATABASE_URL", raising=False)
    assert database_url() == DATABASE_URL == LOCAL_DATABASE_URL
    assert ":" not in DATABASE_URL.split("@", 1)[0].split("//", 1)[1]
    monkeypatch.setenv("NEURO_BOTS_DATABASE_URL", "")
    assert database_url() == DATABASE_URL


def test_adapter_reads_the_configured_key_and_leaves_it_out_of_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "stand-key-" + secrets.token_hex(8)
    monkeypatch.setenv("NEURO_BOTS_AGGREGATOR_KEY", key)
    seen: list[str | None] = []

    def remember() -> str | None:
        value = configured_aggregator_key()
        seen.append(value)
        return value

    monkeypatch.setattr("server.aggregator.configured_aggregator_key", remember)
    text = complete(
        description="",
        scenario="",
        messages=[],
        desktop_available=False,
        page_text="",
    )
    assert text == STUB
    assert key not in text
    assert seen == [key]
    monkeypatch.setenv("NEURO_BOTS_AGGREGATOR_KEY", "")
    assert configured_aggregator_key() is None
