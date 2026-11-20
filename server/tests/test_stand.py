import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
PASSWORD = "abcdef1!"


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


def test_stand_serves_cabinet_and_json_on_one_origin_with_secure_session() -> None:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["NEURO_BOTS_STAND"] = "1"
    env.pop("NEURO_BOTS_DATABASE_URL", None)
    env.pop("NEURO_BOTS_AGGREGATOR_KEY", None)
    env.pop("NEURO_BOTS_MAIL_SINK", None)
    process: subprocess.Popen[str] | None = None
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout
        process = subprocess.Popen(
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
        status, body = _wait_for_http(f"{origin}/health")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}

        cabinet_status, cabinet_body, cabinet_cookie = _open(
            urllib.request.Request(f"{origin}/")
        )
        assert cabinet_status == 200
        assert b'id="app"' in cabinet_body
        assert cabinet_cookie == ""

        me_status, me_body, _me_cookie = _open(
            urllib.request.Request(f"{origin}/api/me")
        )
        assert me_status == 401
        assert json.loads(me_body)["code"] == "unauthenticated"

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
    finally:
        journal = ""
        if process is not None:
            journal = _stop(process)
        _compose("down", "--remove-orphans", timeout=60)
    assert PASSWORD not in journal
    assert session_id not in journal
