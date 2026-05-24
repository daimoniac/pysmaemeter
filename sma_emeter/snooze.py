from sma_emeter.config import SNOOZE_WINDOW


def is_snooze_time() -> bool:
    """True when the current local time is inside the configured snooze window."""
    if SNOOZE_WINDOW is None:
        return False
    return SNOOZE_WINDOW.is_active()
