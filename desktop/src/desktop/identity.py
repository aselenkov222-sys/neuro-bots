import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

PULSE_INTERVAL = timedelta(seconds=30)
MACOS_ZONEINFO_PREFIX = "/var/db/timezone/zoneinfo/"


def computer_id(directory: Path) -> uuid.UUID:
    path = directory / "desktop-id"
    if path.is_file():
        return uuid.UUID(path.read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=True)
    value = uuid.uuid4()
    path.write_text(str(value), encoding="utf-8")
    return value


def default_state_dir() -> Path:
    configured = os.environ.get("NEURO_BOTS_STATE_DIR")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "neuro-bots"
    return Path.home() / ".local" / "share" / "neuro-bots"


def pulse_due(last_sent_at: datetime | None, now: datetime) -> bool:
    if last_sent_at is None:
        return True
    return now - last_sent_at >= PULSE_INTERVAL


def timezone_name_accepted(name: str) -> bool:
    if name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _zone_name_after(value: str, marker: str) -> str | None:
    index = value.rfind(marker)
    if index < 0:
        return None
    name = value[index + len(marker) :]
    if name and timezone_name_accepted(name):
        return name
    return None


def _symlink_target(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.normpath(os.path.join(parent, target))


def timezone_name_from_zoneinfo_path(path: Path) -> str | None:
    linked = _symlink_target(path)
    if linked is not None:
        named = _zone_name_after(linked, MACOS_ZONEINFO_PREFIX)
        if named is not None:
            return named
    return _zone_name_after(os.path.realpath(path), "zoneinfo/")


def local_timezone_name() -> str:
    configured = os.environ.get("TZ")
    if configured is not None:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()
        name = configured[1:] if configured.startswith(":") else configured
        if timezone_name_accepted(name):
            return name
    from_file = timezone_name_from_zoneinfo_path(Path("/etc/localtime"))
    if from_file is not None:
        return from_file
    return "UTC"


def bridge_script(computer: uuid.UUID, timezone_name: str) -> str:
    payload = json.dumps({"id": str(computer), "timezone": timezone_name})
    return (
        "window.__neuroDesktop = "
        + payload
        + ";window.dispatchEvent(new Event('neuro-desktop'));"
    )
