import http.client
import json
import subprocess
import uuid
from pathlib import Path

from desktop.notify import (
    COMPLETED,
    FAILED,
    PHRASES,
    SKIPPED,
    bind_cabinet,
    click_command,
    notification_command,
    open_chat_script,
    post_local,
    reset_cabinet,
)
from desktop.password_screen import LOOPBACK, serve_password_screen
from desktop.routine_gate import mark_window_hidden, reset, window_hidden

WEB_APP = Path(__file__).resolve().parents[2] / "web" / "app.js"


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
        notification_command(PHRASES[COMPLETED], bot_id),
        notification_command(PHRASES[FAILED], bot_id),
        notification_command(PHRASES[SKIPPED], bot_id),
    ]
    assert PHRASES[COMPLETED] == "Запуск завершился"
    assert PHRASES[FAILED] == "Запуск сорвался"
    assert PHRASES[SKIPPED] == "Запуск пропущен"


def test_an_ordinary_reply_and_a_queue_note_are_not_posted() -> None:
    commands: list[list[str]] = []
    bot_id = str(uuid.uuid4())
    posted = post_local(
        [
            _notice("routine_queue", bot_id),
            _notice("reply", bot_id),
        ],
        run=_run(commands),
    )
    assert posted == []
    assert commands == []


def test_clicking_the_posted_notification_opens_that_main_chat() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    assert 'addEventListener("neuro-open-chat"' in source
    assert "__neuroOpenChat" in source
    assert 'location.hash = "#chat"' in source
    bot_id = str(uuid.uuid4())
    other = str(uuid.uuid4())
    commands: list[list[str]] = []
    posted = post_local(
        [_notice(COMPLETED, bot_id), _notice(FAILED, other)],
        run=_run(commands),
    )
    assert [item["bot_id"] for item in posted] == [bot_id, other]
    assert bot_id in commands[0][2]
    assert other not in commands[0][2]
    hidden = _click_recorded(commands[0], hidden=True)
    visible = _click_recorded(commands[1], hidden=False)
    assert hidden == [open_chat_script(bot_id)]
    assert visible == [open_chat_script(other)]


def _click_recorded(command: list[str], *, hidden: bool) -> list[str]:
    scripts: list[str] = []
    shown: list[str] = []
    reset()
    reset_cabinet()
    if hidden:
        mark_window_hidden()
    bind_cabinet(lambda: shown.append("shown"), scripts.append)
    assert click_command(command) is True
    assert shown == (["shown"] if hidden else [])
    assert window_hidden() is False
    return scripts


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
        shown: list[str] = []
        scripts: list[str] = []
        reset()
        reset_cabinet()
        mark_window_hidden()
        bind_cabinet(lambda: shown.append("shown"), scripts.append)
        assert click_command(commands[0]) is True
        assert shown == ["shown"]
        assert window_hidden() is False
        assert scripts == [open_chat_script(bot_id)]
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
    return _post_path(opened, "/notify", payload, origin)


def _post_path(opened, path: str, payload: dict, origin: str) -> tuple[int, bytes]:
    body = json.dumps(payload).encode()
    connection = http.client.HTTPConnection(LOOPBACK, opened.port, timeout=5)
    try:
        connection.request(
            "POST",
            path,
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
