import time
from datetime import datetime, timedelta, timezone

import pytest

from desktop.identity import (
    PULSE_INTERVAL,
    bridge_script,
    local_timezone_name,
    pulse_due,
)


def test_pulse_is_due_at_thirty_seconds_and_not_before() -> None:
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    assert PULSE_INTERVAL == timedelta(seconds=30)
    assert pulse_due(None, now) is True
    assert pulse_due(now - timedelta(seconds=29), now) is False
    assert pulse_due(now - timedelta(seconds=30), now) is True


def test_local_timezone_name_follows_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Moscow")
    try:
        assert local_timezone_name() == "Europe/Moscow"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_bridge_script_carries_only_the_id_and_timezone() -> None:
    import json
    import uuid

    computer = uuid.uuid4()
    script = bridge_script(computer, "Europe/Moscow")
    payload = script.split("window.__neuroDesktop = ", 1)[1].split(
        ";window.dispatchEvent", 1
    )[0]
    assert json.loads(payload) == {"id": str(computer), "timezone": "Europe/Moscow"}
    assert set(json.loads(payload)) == {"id", "timezone"}
