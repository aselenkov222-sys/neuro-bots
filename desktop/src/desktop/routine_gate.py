_window_hidden = False
_process_stopping = False


def reset() -> None:
    global _window_hidden, _process_stopping
    _window_hidden = False
    _process_stopping = False


def mark_window_hidden() -> None:
    global _window_hidden
    _window_hidden = True


def mark_window_shown() -> None:
    global _window_hidden
    _window_hidden = False


def window_hidden() -> bool:
    return _window_hidden


def mark_process_stopping() -> None:
    global _process_stopping
    _process_stopping = True


def routine_run_allowed() -> bool:
    return not _process_stopping


def claim_if_allowed(claim) -> bool:
    if not routine_run_allowed():
        return False
    claim()
    return True
