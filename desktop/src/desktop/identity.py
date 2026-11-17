import json
import os
import sys
import uuid
from pathlib import Path


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


def bridge_script(computer: uuid.UUID) -> str:
    payload = json.dumps({"id": str(computer)})
    return (
        "window.__neuroDesktop = "
        + payload
        + ";window.dispatchEvent(new Event('neuro-desktop'));"
    )
