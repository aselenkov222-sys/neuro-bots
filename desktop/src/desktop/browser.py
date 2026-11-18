import os
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from desktop.identity import default_state_dir

_held: list[object] = []


def browser_profile_dir() -> Path:
    return default_state_dir() / "browser-profile"


def _windows_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    drive = os.environ.get("HOMEDRIVE")
    if drive:
        roots.append(Path(drive) / "Program Files")
        roots.append(Path(drive) / "Program Files (x86)")
    return tuple(roots)


def _chrome_executables() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
    if sys.platform == "win32":
        return tuple(
            root / "Google" / "Chrome" / "Application" / "chrome.exe"
            for root in _windows_roots()
        )
    return (Path("/opt/google/chrome/chrome"),)


def _edge_executables() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),)
    if sys.platform == "win32":
        return tuple(
            root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            for root in _windows_roots()
        )
    return (Path("/opt/microsoft/msedge/msedge"),)


def _chromium_executables() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),)
    if sys.platform == "win32":
        return tuple(
            root / "Chromium" / "Application" / "chrome.exe"
            for root in _windows_roots()
        )
    return (Path("/usr/lib/chromium/chromium"),)


def _executable_installed(paths: tuple[Path, ...]) -> bool:
    return any(path.is_file() for path in paths)


def _chrome_installed() -> bool:
    return _executable_installed(_chrome_executables())


def _edge_installed() -> bool:
    return _executable_installed(_edge_executables())


def _chromium_executable() -> Path | None:
    for path in _chromium_executables():
        if path.is_file():
            return path
    return None


def open_browser() -> BrowserContext | None:
    if _chrome_installed():
        launch: dict[str, str | Path] = {"channel": "chrome"}
    elif _edge_installed():
        launch = {"channel": "msedge"}
    else:
        executable = _chromium_executable()
        if executable is None:
            return None
        launch = {"executable_path": executable}
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(profile, **launch)
    _held.append((playwright, context))
    return context
