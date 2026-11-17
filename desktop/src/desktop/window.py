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
