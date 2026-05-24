import json
import logging
import time
from typing import Any, Dict

from sma_emeter.config import CONFIG, INVALID_DAILY_YIELD_VALUES, TOTAL_COUNTER_PATH


def _current_date() -> str:
    return time.strftime('%Y-%m-%d', time.localtime())


def _parse_non_negative_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0")
    return parsed


def load_total_counter_state() -> Dict[str, Any]:
    """Loads persisted total counter state from disk with safe defaults."""
    default_state = {
        'date': _current_date(),
        'day_max_wh': 0,
        'accumulated_wh': 0,
    }

    if not TOTAL_COUNTER_PATH.exists():
        return default_state

    try:
        with open(TOTAL_COUNTER_PATH, 'r') as f:
            raw = json.load(f)
        return {
            'date': str(raw.get('date', default_state['date'])),
            'day_max_wh': _parse_non_negative_int(raw.get('day_max_wh', 0), 'day_max_wh'),
            'accumulated_wh': _parse_non_negative_int(raw.get('accumulated_wh', 0), 'accumulated_wh'),
        }
    except Exception as e:
        logging.warning(
            "Invalid total counter state file %s: %s. Resetting state.",
            TOTAL_COUNTER_PATH,
            e,
        )
        return default_state


def save_total_counter_state(state: Dict[str, Any]) -> None:
    """Saves total counter state atomically to avoid partial writes."""
    tmp_path = TOTAL_COUNTER_PATH.with_suffix('.json.tmp')
    serialized = {
        'date': str(state['date']),
        'day_max_wh': _parse_non_negative_int(state['day_max_wh'], 'day_max_wh'),
        'accumulated_wh': _parse_non_negative_int(state['accumulated_wh'], 'accumulated_wh'),
    }
    with open(tmp_path, 'w') as f:
        json.dump(serialized, f, indent=2)
        f.write('\n')
    tmp_path.replace(TOTAL_COUNTER_PATH)


def load_total_yield_baseline_wh() -> int:
    """Loads baseline total yield from config key emeter.totalyieldbaseline (kWh)."""
    emeter_config = CONFIG['emeter']
    try:
        baseline_kwh = float(emeter_config['totalyieldbaseline'])
    except (TypeError, ValueError):
        raise ValueError("Config key emeter.totalyieldbaseline must be numeric")
    if baseline_kwh < 0:
        raise ValueError("Config key emeter.totalyieldbaseline must be >= 0")
    return int(baseline_kwh * 1000)


def sanitize_daily_yield(value: Any, device_label: str) -> int:
    """Normalize known invalid daily-yield values to 0."""
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        logging.warning("Invalid daily yield for %s: %r. Using 0.", device_label, value)
        return 0
    if normalized in INVALID_DAILY_YIELD_VALUES:
        logging.warning(
            "Discarding invalid daily yield %d for %s. Using 0.",
            normalized,
            device_label,
        )
        return 0
    # Clamp negative Modbus interpretations (e.g. unsigned wrap).
    return max(0, normalized)


def update_rollover_state(
    state: Dict[str, Any], raw_daily_yield_wh: int, baseline_wh: int
) -> tuple[int, int]:
    """Update daily rollover counters and compute effective yield totals.

    Returns (current_daily_yield_wh, total_emitted_yield_wh) in Wh.
    """
    current_date = _current_date()
    current_daily_yield_wh = sanitize_daily_yield(raw_daily_yield_wh, 'aggregate daily_yield')
    state_changed = False

    if state['date'] != current_date:
        previous_day = state['date']
        previous_day_max_wh = state['day_max_wh']
        state['accumulated_wh'] += previous_day_max_wh
        state['date'] = current_date
        state['day_max_wh'] = 0
        state_changed = True
        current_daily_yield_wh = 0
        logging.info(
            "Daily rollover %s -> %s: added %dWh to accumulated total (%dWh)",
            previous_day,
            current_date,
            previous_day_max_wh,
            state['accumulated_wh'],
        )
    elif current_daily_yield_wh > state['day_max_wh']:
        state['day_max_wh'] = current_daily_yield_wh
        state_changed = True

    if state_changed:
        save_total_counter_state(state)

    effective_daily_yield_wh = max(current_daily_yield_wh, state['day_max_wh'])
    total_emitted_yield_wh = baseline_wh + state['accumulated_wh'] + effective_daily_yield_wh
    logging.debug(
        "Total emitted yield: baseline=%dWh, accumulated=%dWh, "
        "today=%dWh, effective=%dWh, emitted=%dWh",
        baseline_wh,
        state['accumulated_wh'],
        current_daily_yield_wh,
        effective_daily_yield_wh,
        total_emitted_yield_wh,
    )
    return current_daily_yield_wh, total_emitted_yield_wh
