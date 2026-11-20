from desktop.browser import clear_browser_profile
from desktop.identity import default_state_dir
from desktop.vault import clear_local_vault

MARKER_NAME = "account-removed"


def mark_account_removed() -> None:
    directory = default_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MARKER_NAME).write_text("removed\n", encoding="utf-8")


def prepare_launch(run=None) -> None:
    marker = default_state_dir() / MARKER_NAME
    if not marker.is_file():
        return
    clear_local_vault(run=run)
    clear_browser_profile()
    marker.unlink()
