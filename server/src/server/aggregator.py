from typing import NamedTuple


class CompleteResult(NamedTuple):
    text: str
    file: tuple[str, bytes] | None = None


def complete(
    *,
    description: str,
    scenario: str,
    messages: list[dict[str, str]],
    desktop_available: bool,
) -> str:
    return "Сейчас я на заглушке агрегатора и не вызываю модель."


def split_reply(result: str | CompleteResult) -> tuple[str, tuple[str, bytes] | None]:
    if isinstance(result, str):
        return result, None
    return result.text, _accepted_xlsx(result.file)


def _accepted_xlsx(
    file: tuple[str, bytes] | None,
) -> tuple[str, bytes] | None:
    if not isinstance(file, tuple) or len(file) != 2:
        return None
    name, content = file
    if not _plain_xlsx_name(name) or not isinstance(content, bytes) or len(content) == 0:
        return None
    return name, content


def _plain_xlsx_name(name: object) -> bool:
    if not isinstance(name, str) or len(name) < 5:
        return False
    if "/" in name or "\\" in name:
        return False
    return name.endswith(".xlsx")
