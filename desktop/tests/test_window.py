import importlib
import types

import pytest

from desktop.window import PYSIDE6, PYWEBVIEW, window_library

_PYWEBVIEW_PACKAGE = "webview"
_MACOS_WEBKIT_BINDING = "webview.platforms.cocoa"


def test_successful_pywebview_import_selects_pywebview(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        loaded.append(name)
        return types.ModuleType(name)

    monkeypatch.setattr("desktop.window.importlib.import_module", import_module)

    assert window_library() == PYWEBVIEW
    assert loaded == [_PYWEBVIEW_PACKAGE, _MACOS_WEBKIT_BINDING]


def test_missing_pywebview_import_selects_pyside6(monkeypatch: pytest.MonkeyPatch) -> None:
    def import_module(name: str) -> types.ModuleType:
        raise ImportError(name)

    monkeypatch.setattr("desktop.window.importlib.import_module", import_module)

    assert window_library() == PYSIDE6


def test_missing_native_binding_import_selects_pyside6(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        loaded.append(name)
        if name == _MACOS_WEBKIT_BINDING:
            raise ImportError(name)
        return types.ModuleType(name)

    monkeypatch.setattr("desktop.window.importlib.import_module", import_module)

    assert window_library() == PYSIDE6
    assert loaded == [_PYWEBVIEW_PACKAGE, _MACOS_WEBKIT_BINDING]
    import PySide6

    assert PySide6.__version__.startswith("6.12.")


def test_selected_window_library_imports() -> None:
    selected = window_library()
    if selected == PYWEBVIEW:
        assert importlib.metadata.version("pywebview").startswith("6.2.")
        assert importlib.import_module(_PYWEBVIEW_PACKAGE).__name__ == _PYWEBVIEW_PACKAGE
        assert importlib.import_module(_MACOS_WEBKIT_BINDING).__name__ == _MACOS_WEBKIT_BINDING
        return
    assert selected == PYSIDE6
    assert importlib.import_module("PySide6").__version__.startswith("6.12.")
