import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
WEB_DIR = REPO_ROOT / "web"
PROBE_NAME = "probe.txt"
PROBE_BODY = b"served-from-web"


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


def _get(opener: urllib.request.OpenerDirector, url: str) -> tuple[int, bytes]:
    try:
        with opener.open(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _wait_for_http(opener: urllib.request.OpenerDirector, url: str) -> tuple[int, bytes]:
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return _get(opener, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = str(error)
            time.sleep(0.1)
    raise AssertionError(last_error)


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def test_health_and_web_share_one_origin() -> None:
    probe = WEB_DIR / PROBE_NAME
    opener = urllib.request.build_opener()
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[str] | None = None
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        down_status, _down_body = _wait_for_http(opener, f"{origin}/health")
        assert down_status == 503

        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout
        status, body = _get(opener, f"{origin}/health")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}

        probe.write_bytes(PROBE_BODY)
        file_status, file_body = _get(opener, f"{origin}/{PROBE_NAME}")
        assert file_status == 200
        assert file_body == PROBE_BODY
    finally:
        probe.unlink(missing_ok=True)
        if process is not None:
            _stop(process)
        _compose("down", "--remove-orphans", timeout=60)
