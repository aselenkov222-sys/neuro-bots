import os
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parents[1]


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><body>cabinet</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def test_launch_without_origin_exits() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "desktop"],
        cwd=DESKTOP_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0


def test_origin_is_the_launch_argument(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def open_cabinet(origin: str, on_ready=None) -> None:
        seen["origin"] = origin

    monkeypatch.setattr("desktop.window.open_cabinet", open_cabinet)
    from desktop.window import main

    main(["http://127.0.0.1:9/"])
    assert seen["origin"] == "http://127.0.0.1:9/"


def test_pywebview_close_hides_and_quit_command_stops() -> None:
    _assert_driver("pywebview-command", "pywebview")


def test_pywebview_close_hides_and_tray_exit_stops() -> None:
    _assert_driver("pywebview-tray", "pywebview")


def test_failed_bindings_build_pyside6_and_quit_command_stops() -> None:
    _assert_driver("pyside-command", "pyside6")


def test_failed_bindings_build_pyside6_and_tray_exit_stops() -> None:
    _assert_driver("pyside-tray", "pyside6")


def _assert_driver(mode: str, library: str) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode],
        cwd=DESKTOP_ROOT,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ready = next(line for line in result.stdout.splitlines() if line.startswith("READY "))
    assert f"library={library}" in ready
    assert "hidden=1" in ready
    assert "running=1" in ready
    origin = ready.split("origin=", 1)[1].split(" ", 1)[0]
    loaded = ready.split("loaded=", 1)[1].split(" ", 1)[0]
    assert origin.rstrip("/") == loaded.rstrip("/")
    assert origin.startswith("http://127.0.0.1:")


def _drive(mode: str) -> None:
    if mode.startswith("pyside"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        _block_pywebview_import()
    threading.Thread(target=lambda: _fail_later(), daemon=True).start()
    origin = _serve()
    from desktop.window import PYSIDE6, PYWEBVIEW, open_cabinet, window_library

    selected = window_library()
    if mode.startswith("pyside"):
        assert selected == PYSIDE6
        import PySide6

        assert PySide6.__version__.split(".")[:2] == ["6", "12"]
    else:
        assert selected == PYWEBVIEW
        import importlib

        numbers = importlib.metadata.version("pywebview").split(".")
        assert numbers[0] == "6" and numbers[1] == "2"

    def ready(window: object) -> None:
        try:
            _exercise(mode, origin, window)
        except Exception:
            traceback.print_exc()
            os._exit(1)

    open_cabinet(origin, on_ready=ready)


def _fail_later() -> None:
    import time

    time.sleep(25)
    os._exit(1)


def _block_pywebview_import() -> None:
    import importlib

    import desktop.window as window_module

    real_import = importlib.import_module

    def import_module(name: str, package: str | None = None):
        if name == "webview" or name.startswith("webview."):
            raise ImportError(name)
        return real_import(name, package)

    window_module.importlib.import_module = import_module


def _serve() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def _exercise(mode: str, origin: str, window: object) -> None:
    from desktop.window import PYWEBVIEW, window_library

    if window_library() == PYWEBVIEW:
        loaded = _pywebview_loaded(window)
        _hide_pywebview(window)
    else:
        loaded = window.url().toString()
        window.close()
        assert window.isHidden()
    assert loaded.rstrip("/") == origin.rstrip("/")
    library = window_library()
    print(
        f"READY library={library} hidden=1 running=1 origin={origin} loaded={loaded}",
        flush=True,
    )
    if mode.endswith("command"):
        _invoke_command(window)
        return
    _invoke_tray()


def _pywebview_loaded(window: object) -> str:
    loaded = window.get_current_url()
    if not loaded:
        raise AssertionError("window has no url")
    return loaded


def _hide_pywebview(window: object) -> None:
    import importlib
    import threading

    app_helper = importlib.import_module("PyObjCTools.AppHelper")
    cocoa = importlib.import_module("webview.platforms.cocoa")
    done = threading.Event()
    state: dict[str, bool] = {}

    def close() -> None:
        view = next(iter(cocoa.BrowserView.instances.values()))
        view.window.performClose_(None)

        def check() -> None:
            state["visible"] = bool(view.window.isVisible())
            done.set()

        app_helper.callAfter(check)

    app_helper.callAfter(close)
    if not done.wait(5) or state.get("visible", True):
        raise AssertionError("close did not hide the window")


def _invoke_command(window: object) -> None:
    from desktop.window import PYWEBVIEW, _quit_command, command_quit_action, window_library

    if window_library() == PYWEBVIEW:
        action = window.menu[0].items[0]
        if action.title != "Выйти" or action.function is not _quit_command:
            raise AssertionError("quit command is not wired")
        action.function()
        return
    if command_quit_action is None or command_quit_action.text() != "Выйти":
        raise AssertionError("quit command is not wired")
    command_quit_action.trigger()


def _invoke_tray() -> None:
    from desktop.window import PYWEBVIEW, _tray_held, _tray_ready, tray_quit_action, window_library

    if window_library() != PYWEBVIEW:
        if tray_quit_action is None or tray_quit_action.text() != "Выйти":
            raise AssertionError("tray exit is not wired")
        tray_quit_action.trigger()
        return
    if not _tray_ready.wait(5):
        raise AssertionError("tray was not installed")
    _status_item, _menu, command, target = _tray_held[-1]
    title = command.title() if callable(command.title) else command.title
    if title != "Выйти":
        raise AssertionError("tray exit is not wired")
    target.quit_(None)


if __name__ == "__main__":
    _drive(sys.argv[1])
