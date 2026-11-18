import http.client
import json
import subprocess
import uuid

from desktop.notify import COMPLETED, FAILED, PHRASES, SKIPPED, post_local
from desktop.password_screen import LOOPBACK, serve_password_screen


def _run(commands: list[list[str]]):
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def _notice(kind: str, bot_id: str) -> dict[str, str]:
    return {"id": str(uuid.uuid4()), "bot_id": bot_id, "kind": kind}


def test_local_notification_is_recorded_for_completion_failure_and_skip() -> None:
    bot_id = str(uuid.uuid4())
    notices = [
        _notice(COMPLETED, bot_id),
        _notice(FAILED, bot_id),
        _notice(SKIPPED, bot_id),
    ]
    commands: list[list[str]] = []
    posted = post_local(notices, run=_run(commands))
    assert posted == notices
    assert commands == [
        ["osascript", "-e", f'display notification "{PHRASES[COMPLETED]}"'],
        ["osascript", "-e", f'display notification "{PHRASES[FAILED]}"'],
        ["osascript", "-e", f'display notification "{PHRASES[SKIPPED]}"'],
    ]
    assert PHRASES[COMPLETED] == "Запуск завершился"
    assert PHRASES[FAILED] == "Запуск сорвался"
    assert PHRASES[SKIPPED] == "Запуск пропущен"


def test_loopback_posts_those_notifications_and_refuses_a_password() -> None:
    opened = serve_password_screen()
    try:
        cabinet = "http://127.0.0.1:8765"
        opened.httpd.cabinet_origin = cabinet
        commands: list[list[str]] = []
        opened.httpd.notify_run = _run(commands)
        bot_id = str(uuid.uuid4())
        notices = [
            _notice(COMPLETED, bot_id),
            _notice(FAILED, bot_id),
            _notice(SKIPPED, bot_id),
        ]
        status, body = _post(opened, {"notices": notices}, cabinet)
        assert status == 200
        assert json.loads(body.decode()) == {"ids": [item["id"] for item in notices]}
        assert [command[0] for command in commands] == ["osascript", "osascript", "osascript"]
        leaked = {
            "notices": notices,
            "password": "machine-secret-7",
            "cookie": "vault-cookie-9",
        }
        refused, refused_body = _post(opened, leaked, cabinet)
        assert refused == 400
        assert b"machine-secret-7" not in refused_body
        assert b"vault-cookie-9" not in refused_body
        assert len(commands) == 3
    finally:
        opened.close()


def _post(opened, payload: dict, origin: str) -> tuple[int, bytes]:
    body = json.dumps(payload).encode()
    connection = http.client.HTTPConnection(LOOPBACK, opened.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/notify",
            body=body,
            headers={
                "Origin": origin,
                "X-Desktop-Open": opened.open_token,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()
