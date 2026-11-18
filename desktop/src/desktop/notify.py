import json
import subprocess
import sys
import uuid

from desktop.routine_gate import mark_window_shown, window_hidden

COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
PHRASES = {
    COMPLETED: "Запуск завершился",
    FAILED: "Запуск сорвался",
    SKIPPED: "Запуск пропущен",
}


def _uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


_cabinet: dict[str, object] = {"show": None, "run_script": None}


def bind_cabinet(show, run_script) -> None:
    _cabinet["show"] = show
    _cabinet["run_script"] = run_script


def reset_cabinet() -> None:
    _cabinet["show"] = None
    _cabinet["run_script"] = None


def open_chat_script(bot_id: str) -> str:
    parsed = _uuid(bot_id)
    if parsed is None:
        raise ValueError(bot_id)
    return (
        "window.__neuroOpenChat = "
        + json.dumps(parsed)
        + ";window.dispatchEvent(new Event('neuro-open-chat'));"
    )


def activate(bot_id: str) -> bool:
    parsed = _uuid(bot_id)
    show = _cabinet["show"]
    run_script = _cabinet["run_script"]
    if parsed is None or not callable(show) or not callable(run_script):
        return False
    if window_hidden():
        show()
        mark_window_shown()
    run_script(open_chat_script(parsed))
    return True


def post_local(notices: list[dict], run=None) -> list[dict[str, str]]:
    posted: list[dict[str, str]] = []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        kind = notice.get("kind")
        text = PHRASES.get(kind) if isinstance(kind, str) else None
        notice_id = _uuid(notice.get("id"))
        bot_id = _uuid(notice.get("bot_id"))
        if text is None or notice_id is None or bot_id is None:
            continue
        command = ["osascript", "-e", f'display notification "{text}"']
        if run is not None:
            result = run(command)
            if getattr(result, "returncode", 0) != 0:
                continue
        elif sys.platform == "darwin":
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                continue
        posted.append({"id": notice_id, "bot_id": bot_id, "kind": kind})
    return posted
