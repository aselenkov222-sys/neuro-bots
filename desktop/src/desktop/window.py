import importlib.metadata
import os
import sys
import threading

from desktop.identity import (
    bridge_script,
    computer_id,
    default_state_dir,
    local_timezone_name,
)

PYWEBVIEW = "pywebview"
PYSIDE6 = "pyside6"
_QUIT_COMMAND = "Выйти"
_PYWEBVIEW_PACKAGE = "webview"
_MACOS_WEBKIT_BINDING = "webview.platforms.cocoa"

_tray_held: list[object] = []
_tray_installed = False
_tray_ready = threading.Event()
_quit_target_classes: dict[type, type] = {}
command_quit_action = None
tray_quit_action = None


def window_library() -> str:
    if _pywebview_imports():
        return PYWEBVIEW
    return PYSIDE6


def _pywebview_imports() -> bool:
    try:
        importlib.import_module(_PYWEBVIEW_PACKAGE)
        importlib.import_module(_MACOS_WEBKIT_BINDING)
    except ImportError:
        return False
    try:
        version = importlib.metadata.version("pywebview")
    except importlib.metadata.PackageNotFoundError:
        return False
    numbers = version.split(".")
    return len(numbers) >= 2 and numbers[0] == "6" and numbers[1] == "2"


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1 or not argv[0]:
        raise SystemExit("usage: python -m desktop ORIGIN")
    from desktop.password_screen import start_password_screen

    start_password_screen()
    open_cabinet(argv[0])


def open_cabinet(origin: str, on_ready=None) -> None:
    if window_library() == PYWEBVIEW:
        _open_pywebview(origin, on_ready)
        return
    _open_pyside6(origin, on_ready)


def _open_pywebview(origin: str, on_ready) -> None:
    global _tray_installed
    _tray_installed = False
    _tray_ready.clear()
    _stop_app_quit_with_command()
    webview = importlib.import_module(_PYWEBVIEW_PACKAGE)
    menu_module = importlib.import_module("webview.menu")
    window = webview.create_window(
        "Кабинет",
        url=origin,
        menu=[menu_module.Menu("Кабинет", [menu_module.MenuAction(_QUIT_COMMAND, _quit_command)])],
    )
    _hide_pywebview_on_close(window)
    _install_cocoa_quit_tray()
    sent = {"done": False}

    def loaded() -> None:
        _inject_desktop_bridge(window.evaluate_js)
        if on_ready is None or sent["done"]:
            return
        sent["done"] = True
        on_ready(window)

    window.events.loaded += loaded
    webview.start(
        localization={"cocoa.menu.quit": _QUIT_COMMAND, "global.quit": _QUIT_COMMAND},
    )


def _hide_pywebview_on_close(window) -> None:
    def on_closing() -> bool:
        window.hide()
        return False

    window.events.closing += on_closing


def _stop_app_quit_with_command() -> None:
    cocoa = importlib.import_module(_MACOS_WEBKIT_BINDING)
    foundation = importlib.import_module("Foundation")

    def applicationShouldTerminate_(self, app):
        _quit_command()
        return foundation.YES

    cocoa.BrowserView.AppDelegate.applicationShouldTerminate_ = applicationShouldTerminate_


def _install_cocoa_quit_tray() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("the Quit tray item must be created on the main thread")
    _install_cocoa_tray_on_main()


def _install_cocoa_tray_on_main() -> None:
    global _tray_installed, tray_quit_action
    if _tray_installed:
        return
    _tray_installed = True
    appkit = importlib.import_module("AppKit")
    target = _quit_target(appkit.NSObject).alloc().init()
    status_item = appkit.NSStatusBar.systemStatusBar().statusItemWithLength_(
        appkit.NSVariableStatusItemLength
    )
    menu = appkit.NSMenu.alloc().init()
    command = appkit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        _QUIT_COMMAND, "quit:", ""
    )
    command.setTarget_(target)
    menu.addItem_(command)
    status_item.setMenu_(menu)
    button = status_item.button()
    if button is not None:
        button.setTitle_("Кабинет")
    tray_quit_action = command
    _tray_held.append((status_item, menu, command, target))
    _tray_ready.set()


def _open_pyside6(origin: str, on_ready) -> None:
    global command_quit_action, tray_quit_action
    qt_core = importlib.import_module("PySide6.QtCore")
    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    gui = importlib.import_module("PySide6.QtGui")
    pyside = importlib.import_module("PySide6")
    numbers = pyside.__version__.split(".")
    if len(numbers) < 2 or numbers[0] != "6" or numbers[1] != "12":
        raise RuntimeError(pyside.__version__)
    application = widgets.QApplication.instance() or widgets.QApplication([])
    if type(application).__module__.startswith("PySide6"):
        application.setQuitOnLastWindowClosed(False)
    view = _cabinet_view(webengine.QWebEngineView)()
    command_quit_action = _pyside_quit_command(widgets, gui, view)
    tray_quit_action = _install_pyside_quit_tray(widgets, gui, application)
    sent = {"done": False}

    def loaded(_ok: bool) -> None:
        _inject_desktop_bridge(lambda script: view.page().runJavaScript(script))
        if on_ready is None or sent["done"]:
            return
        current = view.url().toString().rstrip("/")
        if current != origin.rstrip("/"):
            return
        sent["done"] = True
        on_ready(view)

    load_finished = getattr(view, "loadFinished", None)
    if load_finished is not None:
        load_finished.connect(loaded)
    view.load(qt_core.QUrl(origin))
    view.show()
    application.exec()


def _cabinet_view(base):
    class CabinetView(base):
        def closeEvent(self, event) -> None:
            event.ignore()
            self.hide()

    CabinetView._qt_base = base
    return CabinetView


def _pyside_quit_command(widgets, gui, view):
    action = gui.QAction(_QUIT_COMMAND)
    action.triggered.connect(_quit_command)
    base = getattr(type(view), "_qt_base", None)
    if base is not None and type(base).__module__.startswith("PySide6"):
        bar = widgets.QMenuBar()
        bar.addMenu("Кабинет").addAction(action)
        bar.show()
        _tray_held.append(bar)
    else:
        menu = widgets.QMenu()
        menu.addAction(action)
        _tray_held.append(menu)
    return action


def _install_pyside_quit_tray(widgets, gui, application):
    tray = widgets.QSystemTrayIcon(application)
    menu = widgets.QMenu()
    command = gui.QAction(_QUIT_COMMAND)
    command.triggered.connect(_quit_from_tray)
    menu.addAction(command)
    tray.setContextMenu(menu)
    tray.setIcon(
        application.style().standardIcon(widgets.QStyle.StandardPixmap.SP_DesktopIcon)
    )
    tray.show()
    _tray_held.append(tray)
    return command


def _quit_target(nsobject: type) -> type:
    cached = _quit_target_classes.get(nsobject)
    if cached is not None:
        return cached

    class QuitTarget(nsobject):
        def quit_(self, _sender) -> None:
            _quit_from_tray()

    _quit_target_classes[nsobject] = QuitTarget
    return QuitTarget


def _quit_command() -> None:
    _stop_process()


def _quit_from_tray() -> None:
    _stop_process()


def _inject_desktop_bridge(run_script) -> None:
    try:
        run_script(
            bridge_script(computer_id(default_state_dir()), local_timezone_name())
        )
    except Exception:
        return


def _stop_process() -> None:
    os._exit(0)
