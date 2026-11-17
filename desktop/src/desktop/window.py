import importlib
import os

PYWEBVIEW = "pywebview"
PYSIDE6 = "pyside6"
_QUIT_COMMAND = "Выйти"
_tray_held: list[object] = []

_PYWEBVIEW_PACKAGE = "webview"
_MACOS_WEBKIT_BINDING = "webview.platforms.cocoa"


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
    return True


def open_cabinet(origin: str) -> None:
    if window_library() == PYWEBVIEW:
        _open_pywebview(origin)
        return
    _open_pyside6(origin)


def _open_pywebview(origin: str) -> None:
    webview = importlib.import_module(_PYWEBVIEW_PACKAGE)
    window = webview.create_window("", url=origin)
    _hide_pywebview_on_close(window)
    webview.start(_install_cocoa_quit_tray)


def _hide_pywebview_on_close(window) -> None:
    def on_closing() -> bool:
        window.hide()
        return False

    window.events.closing += on_closing


def _install_cocoa_quit_tray() -> None:
    appkit = importlib.import_module("AppKit")

    class QuitTarget(appkit.NSObject):
        def quit_(self, _sender) -> None:
            _stop_process()

    target = QuitTarget.alloc().init()
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
        button.setTitle_("·")
    _tray_held.append((status_item, menu, command, target))


def _open_pyside6(origin: str) -> None:
    qt_core = importlib.import_module("PySide6.QtCore")
    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    gui = importlib.import_module("PySide6.QtGui")
    application = widgets.QApplication.instance() or widgets.QApplication([])
    view = _cabinet_view(webengine.QWebEngineView)()
    view.load(qt_core.QUrl(origin))
    _install_pyside_quit_tray(widgets, gui, application)
    view.show()
    application.exec()


def _cabinet_view(base):
    class CabinetView(base):
        def closeEvent(self, event) -> None:
            event.ignore()
            self.hide()

    return CabinetView


def _install_pyside_quit_tray(widgets, gui, application) -> None:
    tray = widgets.QSystemTrayIcon(application)
    menu = widgets.QMenu()
    command = gui.QAction(_QUIT_COMMAND)
    command.triggered.connect(_stop_process)
    menu.addAction(command)
    tray.setContextMenu(menu)
    tray.setIcon(
        application.style().standardIcon(widgets.QStyle.StandardPixmap.SP_DesktopIcon)
    )
    tray.show()
    _tray_held.append(tray)


def _stop_process() -> None:
    os._exit(0)
