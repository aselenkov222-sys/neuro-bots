import subprocess
import sys
import uuid

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
