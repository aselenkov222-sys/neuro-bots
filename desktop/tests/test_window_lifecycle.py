import importlib

import pytest

from desktop.window import PYSIDE6, PYWEBVIEW, open_cabinet


class _CloseEvent:
    def __init__(self) -> None:
        self.ignored = False

    def ignore(self) -> None:
        self.ignored = True


class _PysideView:
    def __init__(self) -> None:
        self.loaded: object = None
        self.hidden = False

    def load(self, url: object) -> None:
        self.loaded = url

    def show(self) -> None:
        return None

    def hide(self) -> None:
        self.hidden = True


class _PysideStyle:
    def standardIcon(self, pixmap: object) -> object:
        return pixmap


class _PysideApplication:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    @staticmethod
    def instance() -> None:
        return None

    def style(self) -> _PysideStyle:
        return _PysideStyle()

    def exec(self) -> int:
        return 0


class _Signal:
    def __init__(self) -> None:
        self.slots: list[object] = []

    def connect(self, slot: object) -> None:
        self.slots.append(slot)


class _Action:
    def __init__(self, text: str) -> None:
        self.text = text
        self.triggered = _Signal()


class _Menu:
    def __init__(self) -> None:
        self.actions: list[_Action] = []

    def addAction(self, action: _Action) -> None:
        self.actions.append(action)


class _Tray:
    def __init__(self, parent: object = None) -> None:
        self.parent = parent
        self.menu: _Menu | None = None

    def setContextMenu(self, menu: _Menu) -> None:
        self.menu = menu

    def setIcon(self, icon: object) -> None:
        self.icon = icon

    def show(self) -> None:
        self.visible = True


def _patch_pyside(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    holder: dict[str, object] = {}

    class View(_PysideView):
        def __init__(self) -> None:
            super().__init__()
            holder["view"] = self

    class Tray(_Tray):
        def __init__(self, parent: object = None) -> None:
            super().__init__(parent)
            holder["tray"] = self

    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    gui = importlib.import_module("PySide6.QtGui")
    monkeypatch.setattr(webengine, "QWebEngineView", View)
    monkeypatch.setattr(widgets, "QApplication", _PysideApplication)
    monkeypatch.setattr(widgets, "QSystemTrayIcon", Tray)
    monkeypatch.setattr(widgets, "QMenu", _Menu)
    monkeypatch.setattr(gui, "QAction", _Action)
    return holder


def _patch_pywebview_start(monkeypatch: pytest.MonkeyPatch, start) -> None:
    webview = importlib.import_module("webview")
    monkeypatch.setattr(webview, "start", start)


def test_pywebview_close_hides_window_and_process_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("desktop.window.window_library", lambda: PYWEBVIEW)
    _patch_pywebview_start(monkeypatch, lambda *args, **kwargs: None)
    webview = importlib.import_module("webview")
    before = list(webview.windows)
    try:
        open_cabinet("http://cabinet.example")
        window = webview.windows[-1]
        hidden: list[str] = []
        window.gui = type("Gui", (), {"hide": lambda self, uid: hidden.append(uid)})()
        window.events.shown.set()
        cancelled = window.events.closing.set()
    finally:
        webview.windows[:] = before

    assert cancelled is True
    assert hidden == [window.uid]


def test_pywebview_quit_command_stops_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("desktop.window.window_library", lambda: PYWEBVIEW)
    recorded: dict[str, object] = {}

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class MenuItem:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithTitle_action_keyEquivalent_(self, title, action, key):
            self.title = title
            self.action = action
            self.key = key
            recorded["command"] = self
            return self

        def setTarget_(self, target) -> None:
            self.target = target

    class Menu:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.items = []
            return self

        def addItem_(self, item) -> None:
            self.items.append(item)

    class StatusItem:
        def __init__(self) -> None:
            self.menu = None

        def setMenu_(self, menu) -> None:
            self.menu = menu

        def button(self):
            return None

    class StatusBar:
        @staticmethod
        def systemStatusBar():
            return StatusBar()

        def statusItemWithLength_(self, length):
            return StatusItem()

    appkit = importlib.import_module("AppKit")
    monkeypatch.setattr(appkit, "NSObject", NSObject)
    monkeypatch.setattr(appkit, "NSMenuItem", MenuItem)
    monkeypatch.setattr(appkit, "NSMenu", Menu)
    monkeypatch.setattr(appkit, "NSStatusBar", StatusBar)
    monkeypatch.setattr(appkit, "NSVariableStatusItemLength", -1)

    def start(func=None, *args, **kwargs) -> None:
        if func is not None:
            func()

    _patch_pywebview_start(monkeypatch, start)
    webview = importlib.import_module("webview")
    before = list(webview.windows)
    codes: list[int] = []
    monkeypatch.setattr("desktop.window.os._exit", lambda code: codes.append(code))
    try:
        open_cabinet("http://cabinet.example")
    finally:
        webview.windows[:] = before

    command = recorded["command"]
    assert command.title == "Выйти"
    command.target.quit_(None)
    assert codes == [0]


def test_pyside6_close_hides_window_and_process_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("desktop.window.window_library", lambda: PYSIDE6)
    holder = _patch_pyside(monkeypatch)
    open_cabinet("http://127.0.0.1:8766")
    view = holder["view"]
    event = _CloseEvent()
    view.closeEvent(event)
    assert event.ignored is True
    assert view.hidden is True


def test_pyside6_quit_command_stops_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("desktop.window.window_library", lambda: PYSIDE6)
    holder = _patch_pyside(monkeypatch)
    codes: list[int] = []
    monkeypatch.setattr("desktop.window.os._exit", lambda code: codes.append(code))
    open_cabinet("http://127.0.0.1:8766")
    menu = holder["tray"].menu
    assert [action.text for action in menu.actions] == ["Выйти"]
    menu.actions[0].triggered.slots[0]()
    assert codes == [0]
