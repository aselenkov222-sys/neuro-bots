import importlib

PYWEBVIEW = "pywebview"
PYSIDE6 = "pyside6"

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
    webview.create_window("", url=origin)
    webview.start()


def _open_pyside6(origin: str) -> None:
    qt_core = importlib.import_module("PySide6.QtCore")
    webengine = importlib.import_module("PySide6.QtWebEngineWidgets")
    widgets = importlib.import_module("PySide6.QtWidgets")
    application = widgets.QApplication.instance() or widgets.QApplication([])
    view = webengine.QWebEngineView()
    view.load(qt_core.QUrl(origin))
    view.show()
    application.exec()
