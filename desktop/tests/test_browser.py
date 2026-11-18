import subprocess
import sys
from pathlib import Path

import pytest

from desktop.browser import browser_profile_dir, open_address, open_browser
from desktop.identity import default_state_dir

_MARKER_NAME = "launch-marker"
_MARKER_TEXT = "kept"


def _record_persistent_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[object, dict[str, object]]]:
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
    return calls


def _installed(
    monkeypatch: pytest.MonkeyPatch, *, chrome: bool, edge: bool
) -> None:
    monkeypatch.setattr("desktop.browser._chrome_installed", lambda: chrome)
    monkeypatch.setattr("desktop.browser._edge_installed", lambda: edge)


def test_open_browser_launches_installed_chrome_in_the_app_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    _installed(monkeypatch, chrome=True, edge=True)
    calls = _record_persistent_launch(monkeypatch)
    open_browser()
    profile = browser_profile_dir()
    assert default_state_dir() == tmp_path
    assert profile.parent == tmp_path
    assert profile.is_dir()
    assert calls == [(profile, {"channel": "chrome"})]


def test_edge_is_selected_when_chrome_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    _installed(monkeypatch, chrome=False, edge=True)
    calls = _record_persistent_launch(monkeypatch)
    open_browser()
    profile = browser_profile_dir()
    assert calls == [(profile, {"channel": "msedge"})]
    assert [path.name for path in tmp_path.iterdir()] == ["browser-profile"]


def test_other_chromium_is_selected_when_chrome_and_edge_are_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.browser.sys.platform", "darwin")
    _installed(monkeypatch, chrome=False, edge=False)
    chromium = Path("/Applications/Chromium.app/Contents/MacOS/Chromium")
    monkeypatch.setattr("desktop.browser._chromium_executable", lambda: chromium)
    calls = _record_persistent_launch(monkeypatch)
    open_browser()
    profile = browser_profile_dir()
    assert calls == [(profile, {"executable_path": chromium})]
    assert [path.name for path in tmp_path.iterdir()] == ["browser-profile"]


def test_macos_chromium_executable_is_the_installed_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("desktop.browser.sys.platform", "darwin")
    from desktop.browser import _chromium_executables

    assert _chromium_executables() == (
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    )


def test_launch_is_skipped_when_no_chromium_browser_is_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    _installed(monkeypatch, chrome=False, edge=False)
    monkeypatch.setattr("desktop.browser._chromium_executable", lambda: None)
    calls = _record_persistent_launch(monkeypatch)
    assert open_browser() is None
    assert calls == []


def test_second_launch_sees_the_same_profile_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    _installed(monkeypatch, chrome=True, edge=False)
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

browser._chrome_installed = lambda: True
browser._edge_installed = lambda: False
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


def test_open_address_reads_the_page_and_does_not_click_or_read_cookies() -> None:
    calls: list[tuple[object, ...]] = []

    class _Page:
        def goto(self, url: str, *args: object, **kwargs: object) -> None:
            calls.append(("goto", url, args, kwargs))

        def inner_text(self, selector: str) -> str:
            calls.append(("inner_text", selector))
            return "Текст страницы"

        def click(self, *args: object, **kwargs: object) -> None:
            calls.append(("click", args))

        def fill(self, *args: object, **kwargs: object) -> None:
            calls.append(("fill", args))

        def type(self, *args: object, **kwargs: object) -> None:
            calls.append(("type", args))

        def press(self, *args: object, **kwargs: object) -> None:
            calls.append(("press", args))

        def input_value(self, *args: object, **kwargs: object) -> str:
            calls.append(("input_value", args))
            return "vault-pass-9"

        def close(self) -> None:
            calls.append(("close",))

    class _Context:
        def new_page(self) -> _Page:
            return _Page()

        def cookies(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
            calls.append(("cookies",))
            return [{"name": "session", "value": "vault-cookie-9"}]

    import desktop.browser as browser

    held = list(browser._held)
    browser._held[:] = [(None, _Context())]
    try:
        assert open_address("https://example.com/inbox") == "Текст страницы"
        with pytest.raises(ValueError, match="^address$"):
            open_address("https://user:vault-pass-9@example.com/secret")
        with pytest.raises(ValueError, match="^address$"):
            open_address("file:///tmp/vault-cookie-9")
    finally:
        browser._held[:] = held
    assert calls == [
        ("goto", "https://example.com/inbox", (), {}),
        ("inner_text", "body"),
        ("close",),
    ]
    assert "vault-pass-9" not in str(calls)
    assert "vault-cookie-9" not in str(calls)
