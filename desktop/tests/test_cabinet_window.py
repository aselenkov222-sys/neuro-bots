import importlib

import pytest

from desktop.window import PYSIDE6, PYWEBVIEW, open_cabinet


class _RecordingView:
    def __init__(self) -> None:
        self.loaded: object = None
        self.shown = False

    def load(self, url: object) -> None:
        self.loaded = url

    def show(self) -> None:
        self.shown = True


class _RecordingApplication:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.executed = False

    @staticmethod
    def instance() -> None:
        return None

    def exec(self) -> int:
        self.executed = True
        return 0


def _patch_pyside(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    holder: dict[str, object] = {}

    class View(_RecordingView):
        def __init__(self) -> None:
            super().__init__()
            holder["view"] = self

    class Application(_RecordingApplication):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            holder["application"] = self

    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    monkeypatch.setattr(webengine, "QWebEngineView", View)
    monkeypatch.setattr(widgets, "QApplication", Application)
    return holder


def _patch_pywebview(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    webview = importlib.import_module("webview")
    opened: dict[str, object] = {"started": False, "created": []}
    real_create = webview.create_window

    def create_window(*args: object, **kwargs: object) -> object:
        window = real_create(*args, **kwargs)
        created = opened["created"]
        assert isinstance(created, list)
        created.append(window)
        return window

    def start(*args: object, **kwargs: object) -> None:
        opened["started"] = True

    monkeypatch.setattr(webview, "create_window", create_window)
    monkeypatch.setattr(webview, "start", start)
    return opened


def test_pywebview_window_opens_passed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://cabinet.example"
    monkeypatch.setattr("desktop.window.window_library", lambda: PYWEBVIEW)
    opened = _patch_pywebview(monkeypatch)
    pyside = _patch_pyside(monkeypatch)
    webview = importlib.import_module("webview")
    before = list(webview.windows)
    try:
        open_cabinet(origin)
    finally:
        webview.windows[:] = before

    created = opened["created"]
    assert isinstance(created, list)
    assert len(created) == 1
    assert created[0].original_url == origin
    assert opened["started"] is True
    assert "view" not in pyside


def test_pyside6_window_opens_passed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://127.0.0.1:8766"
    monkeypatch.setattr("desktop.window.window_library", lambda: PYSIDE6)
    opened = _patch_pywebview(monkeypatch)
    pyside = _patch_pyside(monkeypatch)

    open_cabinet(origin)

    view = pyside["view"]
    application = pyside["application"]
    assert view.loaded.toString() == origin
    assert view.shown is True
    assert application.executed is True
    assert opened["created"] == []
    assert opened["started"] is False
