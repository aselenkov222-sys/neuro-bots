import subprocess
import sys
from pathlib import Path

import pytest

from desktop.browser import browser_profile_dir, open_browser
from desktop.identity import default_state_dir

_MARKER_NAME = "launch-marker"
_MARKER_TEXT = "kept"


def test_open_browser_launches_installed_chrome_in_the_app_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    calls: list[tuple[object, dict[str, object]]] = []

    class _Chromium:
        def launch(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("chromium.launch")

        def launch_persistent_context(
            self, user_data_dir: object, **kwargs: object
        ) -> object:
            calls.append((user_data_dir, kwargs))
            return object()

    class _Playwright:
        def __init__(self) -> None:
            self.chromium = _Chromium()

    class _Manager:
        def start(self) -> _Playwright:
            return _Playwright()

    monkeypatch.setattr("desktop.browser.sync_playwright", lambda: _Manager())
    open_browser()
    profile = browser_profile_dir()
    assert default_state_dir() == tmp_path
    assert profile.parent == tmp_path
    assert profile.is_dir()
    assert calls == [(profile, {"channel": "chrome"})]


def test_second_launch_sees_the_same_profile_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    calls: list[object] = []

    class _Chromium:
        def launch_persistent_context(
            self, user_data_dir: object, **kwargs: object
        ) -> object:
            calls.append(user_data_dir)
            return object()

    class _Playwright:
        def __init__(self) -> None:
            self.chromium = _Chromium()

    class _Manager:
        def start(self) -> _Playwright:
            return _Playwright()

    monkeypatch.setattr("desktop.browser.sync_playwright", lambda: _Manager())
    open_browser()
    assert len(calls) == 1
    first = Path(str(calls[0]))
    assert first.parent == tmp_path
    (first / _MARKER_NAME).write_text(_MARKER_TEXT, encoding="utf-8")

    script = """
from desktop.browser import browser_profile_dir, open_browser
import desktop.browser as browser

seen = []

class _Chromium:
    def launch_persistent_context(self, user_data_dir, **kwargs):
        seen.append((user_data_dir, kwargs))
        return object()

class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()

class _Manager:
    def start(self):
        return _Playwright()

browser.sync_playwright = lambda: _Manager()
open_browser()
profile = browser_profile_dir()
marker = profile / "launch-marker"
if marker.read_text(encoding="utf-8") != "kept":
    raise SystemExit("marker was wiped")
if seen != [(profile, {"channel": "chrome"})]:
    raise SystemExit("second launch used a different profile")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (first / _MARKER_NAME).read_text(encoding="utf-8") == _MARKER_TEXT
