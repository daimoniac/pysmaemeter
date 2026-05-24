from typing import Dict, Iterable

from sma_emeter.state import sanitize_daily_yield


def distribute_phase_values(total_value: int, p1: int, p2: int, p3: int) -> tuple[int, int, int]:
    total_power = p1 + p2 + p3
    if total_power > 0:
        p1_value = int((p1 / total_power) * total_value)
        p2_value = int((p2 / total_power) * total_value)
        p3_value = total_value - p1_value - p2_value
    else:
        p1_value = total_value // 3
        p2_value = total_value // 3
        p3_value = total_value - p1_value - p2_value
    return p1_value, p2_value, p3_value


def _phase_power(phase_power_data: Dict[str, int], phase: int, total_power: int) -> int:
    key = f'p{phase}_power'
    if key in phase_power_data:
        return phase_power_data[key]
    third = total_power // 3
    return third if phase < 3 else total_power - 2 * third


def extract_phase_data(
    total_power: int,
    daily_yield: int,
    phase_power_data: Dict[str, int],
    device_label: str = '',
) -> Dict[str, int]:
    p1_power = _phase_power(phase_power_data, 1, total_power)
    p2_power = _phase_power(phase_power_data, 2, total_power)
    if 'p3_power' in phase_power_data:
        p3_power = phase_power_data['p3_power']
    else:
        p3_power = total_power - p1_power - p2_power

    if all(phase_power_data.get(f'p{i}_yield') is not None for i in (1, 2, 3)):
        yields = {
            f'p{i}_yield': sanitize_daily_yield(
                phase_power_data[f'p{i}_yield'], f"{device_label} p{i}_yield"
            )
            for i in (1, 2, 3)
        }
    else:
        p1_yield, p2_yield, p3_yield = distribute_phase_values(
            daily_yield, p1_power, p2_power, p3_power
        )
        yields = {'p1_yield': p1_yield, 'p2_yield': p2_yield, 'p3_yield': p3_yield}

    return {
        'p1_power': p1_power,
        'p2_power': p2_power,
        'p3_power': p3_power,
        **yields,
    }


def sum_key(devices: Iterable[Dict[str, int]], key: str) -> int:
    return sum(d.get(key, 0) for d in devices)
