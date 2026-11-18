import json
import re
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
_TITLE = "Кабинет"
_NOTIFICATION = re.compile(
    r'^display notification "(Запуск завершился|Запуск сорвался|Запуск пропущен)" '
    r'with title "Кабинет" subtitle '
    r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"$'
)


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


def _script(text: str, bot_id: str) -> str:
    return (
        f'display notification "{text}" with title "{_TITLE}" subtitle "{bot_id}"'
    )


def notification_command(text: str, bot_id: str) -> list[str] | None:
    parsed = _uuid(bot_id)
    if text not in PHRASES.values() or parsed is None:
        return None
    return ["osascript", "-e", _script(text, parsed)]


def click_command(command: list[str]) -> bool:
    if len(command) != 3 or command[0] != "osascript" or command[1] != "-e":
        return False
    found = _NOTIFICATION.fullmatch(command[2])
    if found is None:
        return False
    return activate(found.group(2))


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
        command = notification_command(text, bot_id)
        if command is None:
            continue
        if run is not None:
            result = run(command)
            if getattr(result, "returncode", 0) != 0:
                continue
        elif not _deliver_foundation(bot_id, text):
            if sys.platform == "darwin":
                result = subprocess.run(command, check=False)
                if result.returncode != 0:
                    continue
        posted.append({"id": notice_id, "bot_id": bot_id, "kind": kind})
    return posted


_foundation_delegate = None


def _foundation_delegate():
    global _foundation_delegate
    if _foundation_delegate is not None:
        return _foundation_delegate
    import Foundation

    class NotificationDelegate(Foundation.NSObject):
        def userNotificationCenter_didActivateNotification_(self, _center, notification):
            info = notification.userInfo()
            bot_id = None if info is None else info.get("bot_id")
            if isinstance(bot_id, str):
                activate(bot_id)

        def userNotificationCenter_shouldPresentNotification_(self, _center, _notification):
            return True

    _foundation_delegate = NotificationDelegate.alloc().init()
    return _foundation_delegate


def _deliver_foundation(bot_id: str, text: str) -> bool:
    try:
        import Foundation
    except ImportError:
        return False
    try:
        notification = Foundation.NSUserNotification.alloc().init()
        notification.setTitle_(text)
        notification.setUserInfo_({"bot_id": bot_id})
        center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter()
        center.setDelegate_(_foundation_delegate())
        center.deliverNotification_(notification)
    except (AttributeError, RuntimeError, TypeError):
        return False
    return True
