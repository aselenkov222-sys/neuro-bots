import importlib
import time
import uuid
from pathlib import Path

import pytest

from desktop.identity import bridge_script, computer_id, default_state_dir
from desktop.window import PYSIDE6, PYWEBVIEW, open_cabinet


def test_computer_id_is_created_once_and_read_back(tmp_path: Path) -> None:
    first = computer_id(tmp_path)
    path = tmp_path / "desktop-id"
    assert path.read_text(encoding="utf-8") == str(first)
    assert computer_id(tmp_path) == first
    assert path.read_text(encoding="utf-8") == str(first)


def test_corrupt_desktop_id_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "desktop-id"
    path.write_text("not-a-uuid", encoding="utf-8")
    with pytest.raises(ValueError):
        computer_id(tmp_path)
    assert path.read_text(encoding="utf-8") == "not-a-uuid"


def test_bridge_script_is_the_id_and_the_event() -> None:
    computer = uuid.uuid4()
    script = bridge_script(computer)
    assert script == (
        'window.__neuroDesktop = {"id": "'
        + str(computer)
        + '"};window.dispatchEvent(new Event(\'neuro-desktop\'));'
    )
    assert "timezone" not in script


def test_default_state_dir_honors_neuro_bots_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chosen = tmp_path / "state"
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(chosen))
    assert default_state_dir() == chosen


class _Signal:
    def __init__(self) -> None:
        self.slots: list[object] = []

    def connect(self, slot: object) -> None:
        self.slots.append(slot)


class _Page:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.fail = False

    def runJavaScript(self, script: str) -> None:
        if self.fail:
            raise RuntimeError("injection failed")
        self.scripts.append(script)


class _View:
    def __init__(self, page: _Page) -> None:
        self.page_object = page
        self.loaded: object = None
        self.loadFinished = _Signal()
        self.shown = False

    def load(self, url: object) -> None:
        self.loaded = url

    def show(self) -> None:
        self.shown = True

    def url(self) -> object:
        return self.loaded

    def page(self) -> _Page:
        return self.page_object


class _Application:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    @staticmethod
    def instance() -> None:
        return None

    def exec(self) -> int:
        return 0

    def style(self) -> "_Style":
        return _Style()


class _Style:
    def standardIcon(self, pixmap: object) -> object:
        return pixmap


class _Action:
    def __init__(self, text: str) -> None:
        self.text = text
        self.triggered = _Signal()


class _Menu:
    def __init__(self) -> None:
        self.actions: list[object] = []

    def addAction(self, action: object) -> None:
        self.actions.append(action)


class _Tray:
    def __init__(self, parent: object = None) -> None:
        self.parent = parent

    def setContextMenu(self, menu: object) -> None:
        self.menu = menu

    def setIcon(self, icon: object) -> None:
        self.icon = icon

    def show(self) -> None:
        self.visible = True


def _patch_pyside(monkeypatch: pytest.MonkeyPatch, page: _Page) -> dict[str, object]:
    holder: dict[str, object] = {}

    class View(_View):
        def __init__(self) -> None:
            super().__init__(page)
            holder["view"] = self

    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    gui = importlib.import_module("PySide6.QtGui")
    monkeypatch.setattr(webengine, "QWebEngineView", View)
    monkeypatch.setattr(widgets, "QApplication", _Application)
    monkeypatch.setattr(widgets, "QSystemTrayIcon", _Tray)
    monkeypatch.setattr(widgets, "QMenu", _Menu)
    monkeypatch.setattr(gui, "QAction", _Action)
    return holder


def test_pyside6_loaded_injects_the_bridge_and_still_calls_on_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.window.window_library", lambda: PYSIDE6)
    page = _Page()
    holder = _patch_pyside(monkeypatch, page)
    origin = "http://127.0.0.1:8766"
    ready: list[object] = []
    open_cabinet(origin, on_ready=ready.append)
    view = holder["view"]
    assert isinstance(view, _View)
    assert view.loaded.toString() == origin
    assert len(view.loadFinished.slots) == 1
    view.loadFinished.slots[0](True)
    computer = uuid.UUID((tmp_path / "desktop-id").read_text(encoding="utf-8"))
    assert page.scripts == [bridge_script(computer)]
    assert ready == [view]
    assert view.loaded.toString() == origin


def test_pyside6_injection_failure_still_calls_on_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.window.window_library", lambda: PYSIDE6)
    page = _Page()
    page.fail = True
    holder = _patch_pyside(monkeypatch, page)
    origin = "http://127.0.0.1:8766"
    ready: list[object] = []
    open_cabinet(origin, on_ready=ready.append)
    view = holder["view"]
    assert isinstance(view, _View)
    view.loadFinished.slots[0](True)
    assert ready == [view]
    assert view.loaded.toString() == origin
    assert page.scripts == []


def test_pywebview_loaded_injects_the_bridge_and_still_calls_on_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.window.window_library", lambda: PYWEBVIEW)
    monkeypatch.setattr("desktop.window._stop_app_quit_with_command", lambda: None)
    monkeypatch.setattr("desktop.window._install_cocoa_quit_tray", lambda: None)
    webview = importlib.import_module("webview")
    monkeypatch.setattr(webview, "start", lambda *args, **kwargs: None)
    scripts: list[str] = []
    ready: list[object] = []
    before = list(webview.windows)
    try:
        open_cabinet("http://cabinet.example", on_ready=ready.append)
        window = webview.windows[-1]

        def evaluate_js(script: str, callback: object = None) -> None:
            scripts.append(script)

        window.evaluate_js = evaluate_js
        window.events.loaded.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ready:
            time.sleep(0.01)
        assert window.original_url == "http://cabinet.example"
    finally:
        webview.windows[:] = before
    computer = uuid.UUID((tmp_path / "desktop-id").read_text(encoding="utf-8"))
    assert scripts == [bridge_script(computer)]
    assert ready


def test_pywebview_injection_failure_still_calls_on_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEURO_BOTS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.window.window_library", lambda: PYWEBVIEW)
    monkeypatch.setattr("desktop.window._stop_app_quit_with_command", lambda: None)
    monkeypatch.setattr("desktop.window._install_cocoa_quit_tray", lambda: None)
    webview = importlib.import_module("webview")
    monkeypatch.setattr(webview, "start", lambda *args, **kwargs: None)
    ready: list[object] = []
    before = list(webview.windows)
    try:
        open_cabinet("http://cabinet.example", on_ready=ready.append)
        window = webview.windows[-1]

        def evaluate_js(script: str, callback: object = None) -> None:
            raise RuntimeError("injection failed")

        window.evaluate_js = evaluate_js
        window.events.loaded.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ready:
            time.sleep(0.01)
        assert window.original_url == "http://cabinet.example"
    finally:
        webview.windows[:] = before
    assert ready
