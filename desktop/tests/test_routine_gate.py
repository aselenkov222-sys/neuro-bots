from desktop.routine_gate import (
    claim_if_allowed,
    mark_process_stopping,
    mark_window_hidden,
    reset,
    routine_run_allowed,
)


def test_hidden_window_allows_a_claim_and_stopping_does_not() -> None:
    reset()
    try:
        seen: list[str] = []

        def claim() -> None:
            seen.append("claimed")

        mark_window_hidden()
        assert routine_run_allowed() is True
        assert claim_if_allowed(claim) is True
        assert seen == ["claimed"]
        mark_process_stopping()
        assert routine_run_allowed() is False
        assert claim_if_allowed(claim) is False
        assert seen == ["claimed"]
    finally:
        reset()
