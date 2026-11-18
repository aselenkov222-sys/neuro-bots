from pathlib import Path

import pytest

from desktop.browser import browser_profile_dir, open_browser
from desktop.identity import default_state_dir


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
