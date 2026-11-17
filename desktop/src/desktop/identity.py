import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

PULSE_INTERVAL = timedelta(seconds=30)


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


def local_timezone_name() -> str:
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()
    key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    configured = os.environ.get("TZ")
    if configured:
        return configured
    return "UTC"


def bridge_script(computer: uuid.UUID, timezone_name: str) -> str:
    payload = json.dumps({"id": str(computer), "timezone": timezone_name})
    return (
        "window.__neuroDesktop = "
        + payload
        + ";window.dispatchEvent(new Event('neuro-desktop'));"
    )
