import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desktop.keychain import KeychainError, RecordRejected, read_password, store_password

LOOPBACK = "127.0.0.1"
_MAX_BODY = 64 * 1024
_NEED = "Нужны сайт, логин и пароль."
_SAVED = "Пароль сохранён на этом компьютере."
_WRITE_FAILED = "Ключница не записала пароль."
_MISSING = "Такой записи нет."
_READ_FAILED = "Ключница не отдала пароль."
_LEAD = "Запись остаётся на этом компьютере."

_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Пароль</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1c2128;
      --muted: #3e4653;
      --orange: #c2410c;
      --panel: rgba(255, 255, 255, 0.76);
      --stroke: rgba(28, 33, 40, 0.16);
      --input: rgba(255, 255, 255, 0.9);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body {
      font-family: Onest, "Segoe UI", "Liberation Sans", sans-serif;
      font-size: 16px;
      line-height: 1.5;
      color: var(--ink);
      background:
        radial-gradient(720px 520px at -8% -12%, rgba(255, 220, 186, 0.95), transparent 62%),
        radial-gradient(680px 520px at 112% 108%, rgba(122, 142, 158, 0.92), transparent 58%),
        linear-gradient(165deg, #e6d3c0 0%, #c5ced6 48%, #95a3b0 100%);
    }
    main {
      width: min(28rem, calc(100% - 2rem));
      margin: 8vh auto;
      padding: 1.5rem;
      background: var(--panel);
      border: 1px solid var(--stroke);
      border-radius: 1.25rem;
      box-shadow: 0 18px 40px rgba(48, 36, 24, 0.16);
    }
    h1 { margin: 0 0 0.25rem; font-size: 1.75rem; font-weight: 560; }
    .lead, #saved, #copy-status { margin: 0.25rem 0 0; }
    .lead { color: var(--muted); }
    #saved:empty, #copy-status:empty { display: none; }
    form { margin-top: 1.25rem; }
    label { display: block; margin: 0.75rem 0 0.25rem; }
    input {
      width: 100%;
      padding: 0.55rem 0.7rem;
      border: 1px solid var(--stroke);
      border-radius: 0.6rem;
      background: var(--input);
      color: inherit;
      font: inherit;
    }
    button {
      margin-top: 1rem;
      padding: 0.55rem 1.1rem;
      border: 0;
      border-radius: 999px;
      background: var(--orange);
      color: #fff7f3;
      font: inherit;
    }
    button:focus, input:focus { outline: 2px solid var(--orange); outline-offset: 2px; }
  </style>
</head>
<body>
  <main>
    <h1>Пароль</h1>
    <p class="lead">__LEAD__</p>
    <p id="saved">__SAVED__</p>
    <form method="post" action="/passwords" autocomplete="off">
      <label>Сайт <input name="site" autocomplete="off" required></label>
      <label>Логин <input name="login" autocomplete="off" required></label>
      <label>Пароль <input name="password" type="password" autocomplete="off" required></label>
      <button type="submit">Сохранить</button>
    </form>
    <form autocomplete="off">
      <label>Сайт <input id="copy-site" autocomplete="off" required></label>
      <label>Логин <input id="copy-login" autocomplete="off" required></label>
      <button type="button" id="copy">Скопировать</button>
      <p id="copy-status"></p>
    </form>
  </main>
  <script>
    document.getElementById("copy").addEventListener("click", function () {
      var site = document.getElementById("copy-site").value;
      var login = document.getElementById("copy-login").value;
      var status = document.getElementById("copy-status");
      if (!site || !login) {
        status.textContent = __NEED_JS__;
        return;
      }
      var query = new URLSearchParams({site: site, login: login});
      fetch("/passwords?" + query.toString()).then(function (response) {
        if (response.status === 404) {
          status.textContent = __MISSING_JS__;
          return null;
        }
        if (!response.ok) {
          status.textContent = __READ_FAILED_JS__;
          return null;
        }
        return response.text();
      }).then(function (secret) {
        if (!secret) return;
        navigator.clipboard.writeText(secret).then(function () {
          status.textContent = __COPIED_JS__;
        }, function () {
          status.textContent = __STAYS_JS__;
        });
      });
    });
  </script>
</body>
</html>
"""


class Screen:
    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
        host, port = httpd.server_address[:2]
        self.httpd = httpd
        self.thread = thread
        self.port = port
        self.url = f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class PasswordServer(ThreadingHTTPServer):
    def __init__(self, handler, run) -> None:
        self.run = run
        super().__init__((LOOPBACK, 0), handler)

    def server_bind(self) -> None:
        if self.server_address[0] != LOOPBACK:
            raise RuntimeError(self.server_address[0])
        super().server_bind()


def serve_password_screen(run=None) -> Screen:
    httpd = PasswordServer(_Handler, run)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return Screen(httpd, thread)


_started: Screen | None = None


def start_password_screen() -> str:
    global _started
    if _started is None:
        _started = serve_password_screen()
    return _started.url


def _page(saved: bool) -> bytes:
    note = _SAVED if saved else ""
    html = (
        _PAGE.replace("__LEAD__", _LEAD)
        .replace("__SAVED__", note)
        .replace("__NEED_JS__", _js(_NEED))
        .replace("__MISSING_JS__", _js(_MISSING))
        .replace("__READ_FAILED_JS__", _js(_READ_FAILED))
        .replace("__COPIED_JS__", _js("Пароль скопирован."))
        .replace("__STAYS_JS__", _js("Пароль остаётся на этом компьютере."))
    )
    return html.encode("utf-8")


def _js(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Handler(BaseHTTPRequestHandler):
    server: PasswordServer

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if not self._allowed():
            self._text(403, "")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            saved = urllib.parse.parse_qs(parsed.query).get("saved", [""])[0] == "1"
            self._bytes(200, _page(saved), "text/html; charset=utf-8")
            return
        if parsed.path == "/passwords":
            self._read(parsed.query)
            return
        self._text(404, "")

    def do_POST(self) -> None:
        body = self._body()
        if not self._allowed():
            self._text(403, "")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/passwords" or body is None:
            self._text(400, _NEED)
            return
        if not _form(self.headers.get("Content-Type", "")):
            self._text(400, _NEED)
            return
        fields = _fields(body)
        try:
            store_password(
                fields.get("site", ""),
                fields.get("login", ""),
                fields.get("password", ""),
                run=self.server.run,
            )
        except RecordRejected:
            self._text(400, _NEED)
            return
        except (KeychainError, OSError):
            self._text(500, _WRITE_FAILED)
            return
        self._bytes(303, b"", "text/plain; charset=utf-8", {"Location": "/?saved=1"})

    def _read(self, query: str) -> None:
        fields = _fields(query)
        try:
            password = read_password(
                fields.get("site", ""),
                fields.get("login", ""),
                run=self.server.run,
            )
        except RecordRejected:
            self._text(400, _NEED)
            return
        except (KeychainError, OSError):
            self._text(500, _READ_FAILED)
            return
        if password is None:
            self._text(404, _MISSING)
            return
        self._text(200, password)

    def _allowed(self) -> bool:
        if self.client_address[0] != LOOPBACK:
            return False
        port = self.server.server_address[1]
        if self.headers.get("Host", "") != f"{LOOPBACK}:{port}":
            return False
        own = f"http://{LOOPBACK}:{port}"
        origin = self.headers.get("Origin")
        if origin is not None and origin != own:
            return False
        referer = self.headers.get("Referer")
        if referer is not None and referer != own and not referer.startswith(own + "/"):
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site not in (None, "same-origin", "none"):
            return False
        return True

    def _body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            return None
        length = int(raw_length)
        if length > _MAX_BODY:
            return None
        return self.rfile.read(length)

    def _text(self, status: int, text: str) -> None:
        self._bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _form(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded"


def _fields(raw: str | bytes) -> dict[str, str]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    else:
        text = raw
    parsed = urllib.parse.parse_qs(text, keep_blank_values=True, encoding="utf-8")
    return {key: values[0] if values else "" for key, values in parsed.items()}
