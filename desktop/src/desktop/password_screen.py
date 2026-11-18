import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desktop.identity import default_state_dir
from desktop.keychain import KeychainError, RecordRejected, read_password, store_password
from desktop.sign_in import perform
from desktop.vault import (
    read_record_password,
    records,
    user_add_from_profile,
    user_add_requested,
    user_delete,
    user_replace,
)

LOOPBACK = "127.0.0.1"
_MAX_BODY = 64 * 1024
_NEED = "Нужны сайт, логин и пароль."
_SAVED = "Пароль сохранён на этом компьютере."
_WRITE_FAILED = "Ключница не записала пароль."
_MISSING = "Такой записи нет."
_READ_FAILED = "Ключница не отдала пароль."
_LEAD = "Запись остаётся на этом компьютере."
_DELETE_FAILED = "Запись не удалена."
_NAMES = "vault-bot-names.json"

_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    .vault-screen {
      width: min(460px, calc(100% - 2rem));
      margin: 0 auto 8vh;
      display: grid;
      gap: 16px;
    }
    .vault {
      width: 100%;
      padding: 22px;
      border-radius: 24px;
      display: grid;
      gap: 12px;
      color: #f4f6f8;
      background: linear-gradient(180deg, #454c56 0%, #2c333c 46%, #1c2229 100%);
      border: 1px solid rgba(255, 255, 255, 0.18);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.32),
        inset 0 -12px 18px rgba(0, 0, 0, 0.22),
        0 16px 30px rgba(20, 16, 12, 0.22);
    }
    .shield {
      width: 48px;
      height: 48px;
      border-radius: 16px;
      display: grid;
      place-items: center;
      color: #f3fff8;
      background: linear-gradient(180deg, #2f9a62, #146b3f 58%, #0e4e2e);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.4), 0 8px 16px rgba(0, 0, 0, 0.28);
    }
    .shield svg, .lock svg {
      width: 22px;
      height: 22px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.6;
      stroke-linejoin: round;
      stroke-linecap: round;
    }
    .vault .meta, .vault .hint { color: #c5ccd4; font-size: 14px; line-height: 1.45; }
    .vault h1 { color: #f4f6f8; font-size: 30px; line-height: 1.15; letter-spacing: -0.03em; }
    .vault .field { display: grid; gap: 6px; margin: 0; color: #c5ccd4; font-size: 14px; font-weight: 600; }
    .vault input, .vault select {
      width: 100%;
      min-height: 44px;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      background: rgba(0, 0, 0, 0.32);
      color: #f4f6f8;
      font: inherit;
    }
    .lock { display: flex; align-items: center; gap: 8px; }
    .lock input { flex: 1; }
    .vault .switch {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      min-height: 44px;
      margin: 0;
      font-weight: 600;
    }
    .vault .switch input {
      position: absolute;
      opacity: 0;
      width: 44px;
      height: 26px;
      min-height: 0;
      margin: 0;
      padding: 0;
    }
    .switch-ui {
      width: 44px;
      height: 26px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.14);
      border: 1px solid rgba(255, 255, 255, 0.24);
      position: relative;
      flex: 0 0 auto;
    }
    .switch-ui::after {
      content: "";
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: #f4f6f8;
      position: absolute;
      top: 2px;
      left: 2px;
    }
    .switch input:checked + .switch-ui { background: #146b3f; border-color: #b7ebc9; }
    .switch input:checked + .switch-ui::after { left: 20px; }
    .vault .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .vault button {
      margin: 0;
      min-height: 44px;
      padding: 0 18px;
      border-radius: 999px;
      font-weight: 600;
    }
    .vault .btn-orange {
      color: #fff7f3;
      background: #c2410c;
      border: 1px solid rgba(255, 255, 255, 0.28);
    }
    .vault .btn-quiet {
      color: #f4f6f8;
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.18);
    }
    .vault-owner { font-weight: 600; }
    #vault-copy-status:empty { display: none; }
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
  <section class="vault-screen">
    <form class="vault raised" method="post" action="/vault" autocomplete="off">
      <span class="shield" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.2-2.8 7-7 9-4.2-2-7-4.8-7-9V6z"/></svg></span>
      <div>
        <p class="meta">Хранилище</p>
        <h1>Записи на этом компьютере</h1>
      </div>
      <label class="field">Сайт <input name="vault_site" autocomplete="off" required></label>
      <label class="field">Логин <input name="vault_login" autocomplete="off" required></label>
      <label class="field">Пароль
        <span class="lock"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg><input name="vault_password" type="password" autocomplete="off" required></span>
      </label>
      __BOT_FIELD__
      <p class="hint">Пока бот не выбран, запись общая. Её можно сделать личной для одного бота до сохранения.</p>
      <div class="actions">
        <button class="btn-orange" type="submit">Добавить запись</button>
      </div>
    </form>
    __VAULT_LIST__
    <p id="vault-copy-status"></p>
  </section>
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
  <script>
    document.querySelectorAll("[data-vault-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        var status = document.getElementById("vault-copy-status");
        var id = button.getAttribute("data-vault-copy");
        if (!status || !id) return;
        fetch("/vault/" + encodeURIComponent(id)).then(function (response) {
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
        self.open_token = httpd.open_token

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class PasswordServer(ThreadingHTTPServer):
    def __init__(self, handler, run) -> None:
        self.run = run
        self.open_token = secrets.token_urlsafe(32)
        self.cabinet_origin: str | None = None
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


def allow_cabinet_origin(origin: str) -> None:
    if _started is None:
        return
    _started.httpd.cabinet_origin = origin.rstrip("/")


def screen_bridge() -> tuple[str, str] | None:
    if _started is None:
        return None
    return _started.url, _started.open_token


def stop_password_screen() -> None:
    global _started
    if _started is None:
        return
    _started.close()
    _started = None


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
        .replace("__VAULT_LIST__", _vault_list())
        .replace("__BOT_FIELD__", _bot_select("", _bot_names()))
    )
    return html.encode("utf-8")


def _js(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Handler(BaseHTTPRequestHandler):
    server: PasswordServer

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_OPTIONS(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in {"/open", "/sign-in", "/bot-names"} or not self._loopback_host():
            self._text(404, "")
            return
        if not self._cabinet_origin():
            self._text(403, "")
            return
        self._open_bytes(204, b"", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        form = _form_query(parsed)
        if form is not None and not self._allowed() and self._form_navigation():
            bot, site, saved = form
            self._bytes(200, _request_page(saved, bot, site), "text/html; charset=utf-8")
            return
        if not self._allowed():
            self._text(403, "")
            return
        if form is not None:
            bot, site, saved = form
            self._bytes(200, _request_page(saved, bot, site), "text/html; charset=utf-8")
            return
        if parsed.path == "/":
            saved = urllib.parse.parse_qs(parsed.query).get("saved", [""])[0] == "1"
            self._bytes(200, _page(saved), "text/html; charset=utf-8")
            return
        if parsed.path == "/passwords":
            self._read(parsed.query)
            return
        target = _vault_target(parsed.path)
        if target is not None and target[0] == "item" and target[1] is not None:
            self._vault_copy(target[1])
            return
        self._text(404, "")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        body = self._body()
        if parsed.path == "/open":
            self._open(body)
            return
        if parsed.path == "/sign-in":
            self._sign_in(body)
            return
        if parsed.path == "/bot-names":
            self._save_bot_names(body)
            return
        if not self._allowed():
            self._text(403, "")
            return
        if parsed.path == "/passwords":
            self._store_password(body)
            return
        if parsed.path == "/vault/request":
            self._vault_request(body)
            return
        target = _vault_target(parsed.path)
        if target is None or body is None or not _form(self.headers.get("Content-Type", "")):
            self._text(400, _NEED)
            return
        kind, record_id = target
        fields = _fields(body)
        if kind == "add":
            self._vault_add(fields)
            return
        if kind == "delete" and record_id is not None:
            self._vault_delete(record_id)
            return
        if kind == "item" and record_id is not None:
            self._vault_replace(record_id, fields)
            return
        self._text(400, _NEED)

    def _store_password(self, body: bytes | None) -> None:
        if body is None or not _form(self.headers.get("Content-Type", "")):
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
        self._see_other()

    def _vault_add(self, fields: dict[str, str]) -> None:
        bot_id = _chosen_bot(fields)
        try:
            if bot_id is None:
                user_add_from_profile(
                    fields.get("vault_site", ""),
                    fields.get("vault_login", ""),
                    fields.get("vault_password", ""),
                    run=self.server.run,
                )
            else:
                user_add_from_profile(
                    fields.get("vault_site", ""),
                    fields.get("vault_login", ""),
                    fields.get("vault_password", ""),
                    bot_id=bot_id,
                    run=self.server.run,
                )
        except RecordRejected:
            self._text(400, _NEED)
            return
        except (KeychainError, OSError):
            self._text(500, _WRITE_FAILED)
            return
        self._see_other()

    def _vault_replace(self, record_id: str, fields: dict[str, str]) -> None:
        bot_id = _chosen_bot(fields)
        try:
            user_replace(
                record_id,
                fields.get("vault_site", ""),
                fields.get("vault_login", ""),
                fields.get("vault_password", ""),
                shared=bot_id is None,
                bot_id=bot_id,
                run=self.server.run,
            )
        except RecordRejected:
            self._text(400, _NEED)
            return
        except (KeychainError, OSError):
            self._text(500, _WRITE_FAILED)
            return
        self._see_other()

    def _vault_delete(self, record_id: str) -> None:
        try:
            user_delete(record_id, run=self.server.run)
        except RecordRejected:
            self._text(404, _MISSING)
            return
        except (KeychainError, OSError):
            self._text(500, _DELETE_FAILED)
            return
        self._see_other()

    def _vault_copy(self, record_id: str) -> None:
        found = next((item for item in records() if item["id"] == record_id), None)
        if found is None:
            self._text(404, _MISSING)
            return
        bot_id = found["bot_id"] if isinstance(found["bot_id"], str) and found["bot_id"] else "screen"
        try:
            password = read_record_password(record_id, bot_id, run=self.server.run)
        except (KeychainError, OSError):
            self._text(500, _READ_FAILED)
            return
        if password is None:
            self._text(404, _MISSING)
            return
        self._text(200, password)

    def _see_other(self) -> None:
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

    def _open(self, body: bytes | None) -> None:
        if not self._loopback_host() or not self._cabinet_origin():
            self._text(403, "")
            return
        if self.headers.get("X-Desktop-Open") != self.server.open_token:
            self._open_bytes(403, b"")
            return
        if body is None:
            self._open_bytes(400, b"")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._open_bytes(400, b"")
            return
        if not isinstance(payload, dict) or set(payload) != {"url"}:
            self._open_bytes(400, b"")
            return
        url = payload.get("url")
        from desktop.browser import http_address, open_address

        if not isinstance(url, str) or http_address(url) is None:
            self._open_bytes(400, b"")
            return
        try:
            text = open_address(url)
        except Exception:
            self._open_bytes(500, b"{}")
            return
        encoded = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        self._open_bytes(200, encoded)

    def _sign_in(self, body: bytes | None) -> None:
        if not self._loopback_host() or not self._cabinet_origin():
            self._text(403, "")
            return
        if self.headers.get("X-Desktop-Open") != self.server.open_token:
            self._open_bytes(403, b"")
            return
        if body is None:
            self._open_bytes(400, b"")
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._open_bytes(400, b"")
            return
        if not isinstance(payload, dict) or "password" in payload or "cookie" in payload:
            self._open_bytes(400, b"")
            return
        keys = set(payload)
        if not {"url", "bot_id", "scenario"} <= keys or not keys <= {"url", "bot_id", "scenario", "login"}:
            self._open_bytes(400, b"")
            return
        url = payload.get("url")
        bot_id = payload.get("bot_id")
        scenario = payload.get("scenario")
        chosen = None
        if "login" in payload:
            chosen = payload.get("login")
            if not isinstance(chosen, str) or chosen == "":
                self._open_bytes(400, b"")
                return
        if not isinstance(url, str) or not isinstance(bot_id, str) or not isinstance(scenario, str):
            self._open_bytes(400, b"")
            return
        try:
            result = perform(
                url,
                bot_id,
                scenario,
                chosen=chosen,
                run=self.server.run,
            )
        except ValueError:
            self._open_bytes(400, b"")
            return
        except Exception:
            self._open_bytes(500, b"{}")
            return
        safe = {
            "text": result.get("text", ""),
            "login": result.get("login", "session"),
        }
        if safe["login"] in {"choose", "form"} and isinstance(result.get("site"), str):
            safe["site"] = result["site"]
        if safe["login"] == "choose" and isinstance(result.get("choices"), list):
            safe["choices"] = result["choices"]
        encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
        self._open_bytes(200, encoded)

    def _save_bot_names(self, body: bytes | None) -> None:
        if not self._loopback_host() or not self._cabinet_origin():
            self._text(403, "")
            return
        if self.headers.get("X-Desktop-Open") != self.server.open_token:
            self._open_bytes(403, b"")
            return
        names = _posted_bot_names(body)
        if names is None:
            self._open_bytes(400, b"")
            return
        try:
            _write_bot_names(names)
        except OSError:
            self._open_bytes(500, b"")
            return
        self._open_bytes(204, b"", "text/plain; charset=utf-8")

    def _vault_request(self, body: bytes | None) -> None:
        if body is None or not _form(self.headers.get("Content-Type", "")):
            self._text(400, _NEED)
            return
        fields = _fields(body)
        bot = _request_token(fields.get("request_bot", ""))
        site = _request_site(fields.get("request_site", ""))
        if bot is None or site is None:
            self._text(400, _NEED)
            return
        try:
            user_add_requested(
                site,
                fields.get("request_login", ""),
                fields.get("request_password", ""),
                bot,
                shared="request_shared" in fields,
                run=self.server.run,
            )
        except RecordRejected:
            self._text(400, _NEED)
            return
        except (KeychainError, OSError):
            self._text(500, _WRITE_FAILED)
            return
        query = urllib.parse.urlencode({"saved": "1", "bot": bot, "site": site})
        self._bytes(303, b"", "text/plain; charset=utf-8", {"Location": "/?" + query})

    def _form_navigation(self) -> bool:
        if self.client_address[0] != LOOPBACK:
            return False
        port = self.server.server_address[1]
        if self.headers.get("Host", "") != f"{LOOPBACK}:{port}":
            return False
        if self.headers.get("Sec-Fetch-Mode") not in (None, "navigate"):
            return False
        if self.headers.get("Sec-Fetch-Dest") not in (None, "document"):
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "cross-site", "same-site"):
            return False
        return True

    def _cabinet_origin(self) -> bool:
        origin = self.headers.get("Origin")
        expected = self.server.cabinet_origin
        return (
            origin is not None
            and expected is not None
            and origin.rstrip("/") == expected
        )

    def _loopback_host(self) -> bool:
        if self.client_address[0] != LOOPBACK:
            return False
        port = self.server.server_address[1]
        return self.headers.get("Host", "") == f"{LOOPBACK}:{port}"

    def _open_bytes(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        origin = self.server.cabinet_origin or ""
        self._bytes(
            status,
            body,
            content_type,
            {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Content-Type, X-Desktop-Open",
                "Access-Control-Allow-Private-Network": "true",
                "Vary": "Origin",
            },
        )

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
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _vault_list() -> str:
    names = _bot_names()
    return "".join(_record_html(record, names) for record in records())


def _record_html(record: dict, names: dict[str, str]) -> str:
    record_id = _esc(str(record["id"]))
    site = _esc(str(record["site"]))
    login = _esc(str(record["login"]))
    label = _esc(_owner_label(record, names))
    bot_id = record["bot_id"] if record["shared"] is False and isinstance(record["bot_id"], str) else ""
    return (
        '<article class="vault raised">'
        f'<p class="vault-site">{site}</p>'
        f'<p class="vault-login">{login}</p>'
        f'<p class="vault-owner">{label}</p>'
        f'<button type="button" data-vault-copy="{record_id}">Скопировать</button>'
        f'<form method="post" action="/vault/{record_id}" autocomplete="off">'
        f'<label class="field">Сайт <input name="vault_site" value="{site}" autocomplete="off" required></label>'
        f'<label class="field">Логин <input name="vault_login" value="{login}" autocomplete="off" required></label>'
        '<label class="field">Пароль <input name="vault_password" type="password" autocomplete="off" required></label>'
        f"{_bot_select(bot_id, names)}"
        '<button class="btn-quiet" type="submit">Заменить</button>'
        "</form>"
        f'<form method="post" action="/vault/{record_id}/delete">'
        '<button class="btn-quiet" type="submit">Удалить</button>'
        "</form>"
        "</article>"
    )


def _owner_label(record: dict, names: dict[str, str]) -> str:
    if record["shared"] is not False:
        return "общая"
    bot_id = record["bot_id"]
    if not isinstance(bot_id, str) or bot_id == "":
        return "общая"
    return names.get(bot_id, bot_id)


def _bot_names() -> dict[str, str]:
    path = default_state_dir() / _NAMES
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    names: dict[str, str] = {}
    for key, value in loaded.items():
        if isinstance(key, str) and key != "" and isinstance(value, str):
            names[key] = value
    return names


def _bot_select(selected: str, names: dict[str, str]) -> str:
    options = [
        '<option value="" selected>общая</option>'
        if selected == ""
        else '<option value="">общая</option>'
    ]
    seen: set[str] = set()
    for bot_id, name in names.items():
        seen.add(bot_id)
        mark = " selected" if bot_id == selected else ""
        options.append(f'<option value="{_esc(bot_id)}"{mark}>{_esc(name)}</option>')
    if selected != "" and selected not in seen:
        options.append(f'<option value="{_esc(selected)}" selected>{_esc(selected)}</option>')
    return (
        '<label class="field">Бот <select name="vault_bot">'
        + "".join(options)
        + "</select></label>"
    )


def _posted_bot_names(body: bytes | None) -> dict[str, str] | None:
    if body is None:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"bots"}:
        return None
    bots = payload["bots"]
    if not isinstance(bots, list):
        return None
    names: dict[str, str] = {}
    for item in bots:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            return None
        bot_id = item["id"]
        name = item["name"]
        if (
            not isinstance(bot_id, str)
            or not isinstance(name, str)
            or bot_id == ""
            or name == ""
            or "\x00" in bot_id
            or "\x00" in name
            or len(bot_id) > 200
            or len(name) > 200
        ):
            return None
        names[bot_id] = name
    return names


def _write_bot_names(names: dict[str, str]) -> None:
    path = default_state_dir() / _NAMES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(names, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _chosen_bot(fields: dict[str, str]) -> str | None:
    bot_id = fields.get("vault_bot", "")
    if bot_id == "":
        return None
    return bot_id


def _vault_target(path: str) -> tuple[str, str | None] | None:
    parts = path.split("/")
    if parts == ["", "vault"]:
        return ("add", None)
    if len(parts) == 3 and parts[0] == "" and parts[1] == "vault":
        record_id = _safe_id(parts[2])
        if record_id is None:
            return None
        return ("item", record_id)
    if len(parts) == 4 and parts[:2] == ["", "vault"] and parts[3] == "delete":
        record_id = _safe_id(parts[2])
        if record_id is None:
            return None
        return ("delete", record_id)
    return None


def _safe_id(segment: str) -> str | None:
    if segment == "" or len(segment) > 80:
        return None
    if any(char not in "0123456789abcdefABCDEF-" for char in segment):
        return None
    return segment


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _form_query(parsed) -> tuple[str, str, bool] | None:
    if parsed.path != "/":
        return None
    query = urllib.parse.parse_qs(parsed.query)
    bot = _request_token(query.get("bot", [""])[0])
    site = _request_site(query.get("site", [""])[0])
    if bot is None or site is None:
        return None
    return bot, site, query.get("saved", [""])[0] == "1"


def _request_token(value: str) -> str | None:
    if value == "" or len(value) > 80:
        return None
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for char in value):
        return None
    return value


def _request_site(value: str) -> str | None:
    if value == "" or len(value) > 253:
        return None
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for char in value):
        return None
    if value.startswith(".") or value.endswith(".") or ".." in value:
        return None
    return value


def _request_page(saved: bool, bot: str, site: str) -> bytes:
    site_html = _esc(site)
    bot_html = _esc(bot)
    if saved:
        body = (
            '<p id="saved">'
            + _SAVED
            + '</p><p id="request-saved" data-site="'
            + site_html
            + '" hidden></p>'
        )
    else:
        body = (
            '<form class="vault raised" method="post" action="/vault/request" autocomplete="off">'
            '<span class="shield" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.2-2.8 7-7 9-4.2-2-7-4.8-7-9V6z"/></svg></span>'
            "<div><p class=\"meta\">Защищённая форма</p><h1>Доступ для бота</h1></div>"
            '<label class="field">Сайт <input name="request_site" value="'
            + site_html
            + '" readonly></label>'
            '<label class="field">Логин <input name="request_login" autocomplete="off" required></label>'
            '<label class="field">Пароль <span class="lock"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>'
            '<input name="request_password" type="password" autocomplete="off" required></span></label>'
            '<label class="switch"><input name="request_shared" type="checkbox" value="1">'
            '<span class="switch-ui" aria-hidden="true"></span>Сделать запись общей</label>'
            '<p class="hint">Пока переключатель выключен, запись личная для этого бота. Пароль в чат не попадает. После сохранения там будет только факт, что доступ сохранён.</p>'
            '<input type="hidden" name="request_bot" value="'
            + bot_html
            + '">'
            '<div class="actions"><button class="btn-orange" type="submit">Сохранить доступ</button></div>'
            "</form>"
        )
    html = (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Доступ</title><style>"
        "body{font-family:Onest,\"Segoe UI\",\"Liberation Sans\",sans-serif;background:#c5ced6;color:#1c2128;}"
        ".vault-screen{width:min(460px,calc(100% - 2rem));margin:8vh auto;}"
        ".vault{padding:22px;border-radius:24px;display:grid;gap:12px;color:#f4f6f8;"
        "background:linear-gradient(180deg,#454c56 0%,#2c333c 46%,#1c2229 100%);}"
        ".shield{width:48px;height:48px;border-radius:16px;display:grid;place-items:center;color:#f3fff8;"
        "background:linear-gradient(180deg,#2f9a62,#146b3f 58%,#0e4e2e);}"
        ".shield svg,.lock svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.6;}"
        ".vault h1{margin:0;font-size:30px;}.vault .meta,.vault .hint{color:#c5ccd4;}"
        ".field{display:grid;gap:6px;}.vault input{width:100%;min-height:44px;padding:12px 14px;"
        "border-radius:12px;border:1px solid rgba(255,255,255,.16);background:rgba(0,0,0,.32);color:#f4f6f8;}"
        ".lock{display:flex;align-items:center;gap:8px;}.lock input{flex:1;}"
        ".switch{display:inline-flex;align-items:center;gap:10px;min-height:44px;}"
        ".switch input{width:44px;height:26px;}.switch-ui{width:44px;height:26px;border-radius:999px;"
        "background:rgba(255,255,255,.14);display:inline-block;}"
        ".btn-orange{min-height:44px;border:0;border-radius:999px;background:#c2410c;color:#fff7f3;padding:0 18px;}"
        "</style></head><body><section class=\"vault-screen\">"
        + body
        + "</section><script>"
        "var savedAccess=document.getElementById(\"request-saved\");"
        "if(savedAccess){var savedSite=savedAccess.getAttribute(\"data-site\");"
        "var notify=window.opener;if(!notify&&window.parent!==window)notify=window.parent;"
        "if(savedSite&&notify)notify.postMessage({login:\"saved\",site:savedSite},\"*\");}"
        "</script></body></html>"
    )
    return html.encode("utf-8")


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
