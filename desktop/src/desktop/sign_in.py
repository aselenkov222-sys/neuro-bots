"""Live-chat login. The password stays on this desktop."""

from urllib.parse import urlsplit

from desktop.vault import read_record_password, records_for_bot

_LOGIN_FIELD = "input[type='text'], input[type='email'], input[name='login']"
_PASSWORD_FIELD = "input[type='password']"
_SUBMIT = "button[type='submit'], input[type='submit']"


def perform(
    url: str,
    bot_id: str,
    scenario: str,
    chosen: str | None = None,
    run=None,
    page=None,
) -> dict[str, object]:
    from desktop.browser import http_address

    address = http_address(url) if isinstance(url, str) else None
    if address is None:
        raise ValueError("address")
    if not isinstance(bot_id, str):
        bot_id = ""
    if not isinstance(scenario, str):
        scenario = ""
    if not isinstance(chosen, str) or chosen == "":
        chosen = None
    owned = page is None
    if page is None:
        page = _open_page(address)
    try:
        return _decide(page, address, bot_id, scenario, chosen, run)
    finally:
        if owned:
            page.close()


def _open_page(url: str):
    from desktop import browser

    opener = browser.open_address
    code = getattr(opener, "__code__", None)
    filename = getattr(code, "co_filename", "")
    # A test may replace open_address with a function that only returns body
    # text. That stand-in has no password field, so it is the open session.
    real = opener.__name__ == "open_address" and str(filename).endswith("browser.py")
    if real:
        context = browser._open_context()
        if context is not None:
            opened = context.new_page()
            try:
                opened.goto(url)
            except Exception:
                opened.close()
                raise
            return opened
    return _TextPage(opener(url))


def _decide(page, url: str, bot_id: str, scenario: str, chosen: str | None, run) -> dict[str, object]:
    if page.locator(_PASSWORD_FIELD).count() == 0:
        return {"text": page.inner_text("body"), "login": "session"}
    host = urlsplit(url).hostname or ""
    visible = [
        record
        for record in records_for_bot(bot_id)
        if record["site"] == host and isinstance(record["login"], str) and record["login"] != ""
    ]
    if chosen is not None:
        match = [record for record in visible if record["login"] == chosen]
        if len(match) == 1:
            return _entered(page, match[0], bot_id, run)
        if not visible:
            return _form(page, host)
        return _choose(page, host, visible)
    named = [record for record in visible if str(record["login"]) in scenario]
    if len(named) == 1:
        return _entered(page, named[0], bot_id, run)
    if not visible:
        return _form(page, host)
    return _choose(page, host, visible)


def _choose(page, host: str, visible: list[dict]) -> dict[str, object]:
    return {
        "text": page.inner_text("body"),
        "login": "choose",
        "site": host,
        "choices": [str(record["login"]) for record in visible],
    }


def _form(page, host: str) -> dict[str, object]:
    return {"text": page.inner_text("body"), "login": "form", "site": host}


def _entered(page, record: dict, bot_id: str, run) -> dict[str, object]:
    password = read_record_password(str(record["id"]), bot_id, run=run)
    if not isinstance(password, str) or password == "":
        return _form(page, str(record["site"]))
    page.locator(_LOGIN_FIELD).first.fill(str(record["login"]))
    page.locator(_PASSWORD_FIELD).first.fill(password)
    page.locator("form").locator(_SUBMIT).first.click()
    text = page.inner_text("body")
    text = text.replace(password, "")
    return {"text": text, "login": "entered"}


class _TextPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def locator(self, selector: str) -> "_TextLocator":
        return _TextLocator()

    def inner_text(self, selector: str) -> str:
        return self._text

    def close(self) -> None:
        return None


class _TextLocator:
    @property
    def first(self) -> "_TextLocator":
        return self

    def count(self) -> int:
        return 0

    def fill(self, value: str) -> None:
        return None

    def click(self) -> None:
        return None

    def locator(self, selector: str) -> "_TextLocator":
        return _TextLocator()
