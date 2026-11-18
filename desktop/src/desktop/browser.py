from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from desktop.identity import default_state_dir

_held: list[object] = []


def browser_profile_dir() -> Path:
    return default_state_dir() / "browser-profile"


def open_browser() -> BrowserContext:
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        profile,
        channel="chrome",
    )
    _held.append((playwright, context))
    return context
