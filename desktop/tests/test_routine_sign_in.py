import json

import pytest

from desktop.routine_sign_in import failure_reason
from desktop.vault import read_record_password


ANNA = "anna@example.com"
BORIS = "boris@example.com"


def test_choose_is_sent_as_vault_choice_without_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: object, **kwargs: object) -> str:
        raise AssertionError("password was read")

    monkeypatch.setattr("desktop.vault.read_record_password", blocked)
    result = {
        "text": f"Какую запись использовать для mail.example? {ANNA} или {BORIS}.",
        "login": "choose",
        "site": "mail.example",
        "choices": [ANNA, BORIS],
    }
    sent = failure_reason(result)
    assert sent == "vault_choice"
    encoded = json.dumps(sent, ensure_ascii=False)
    assert ANNA not in encoded
    assert BORIS not in encoded
    assert "Какую запись" not in encoded
    assert read_record_password is not failure_reason
