import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Any, Dict, Optional, Tuple

from sma_emeter.config import (
    CONFIG,
    MODBUS_DEVICE_TYPE,
    PHASE_METRIC_KEYS,
    SMA_DEVICES,
    SPEEDWIRE_DEVICE_TYPE,
)
from sma_emeter.device_state import DeviceCollectionState
from sma_emeter.modbus_reader import ModbusClientPool
from sma_emeter.phase_data import extract_phase_data, sum_key
from sma_emeter.speedwire import SpeedwireCollector
from sma_emeter.state import sanitize_daily_yield


def _device_read_timeout_seconds() -> float:
    modbus_timeout = float(CONFIG.get('modbus', {}).get('timeout', 1))
    speedwire_timeout = float(CONFIG.get('speedwire', {}).get('timeout', 1))
    return max(modbus_timeout, speedwire_timeout) + 0.5


def _collect_device_raw(
    device_id: str,
    device_info: Dict[str, Any],
    modbus_pool: ModbusClientPool,
    speedwire: SpeedwireCollector,
) -> Optional[Dict[str, int]]:
    """Read one device; returns None when no reply was received."""
    device_label = f"device {device_id} ({device_info['name']})"
    device_type = device_info['type']

    if device_type == MODBUS_DEVICE_TYPE:
        data = modbus_pool.read_device(device_id, device_type)
        if data is None:
            return None
        total_power = data['total_power']
        daily_yield = sanitize_daily_yield(data['daily_yield'], device_label)
        phase_power_data = {k: data[k] for k in ('p1_power', 'p2_power', 'p3_power')}

    elif device_type == SPEEDWIRE_DEVICE_TYPE:
        data = speedwire.fetch()
        if data is None:
            return None
        total_power = data.get('spotacpower', 0)
        daily_yield = sanitize_daily_yield(data.get('tagesertrag', 0), device_label)
        phase_power_data = data
    else:
        return None

    phase_data = extract_phase_data(total_power, daily_yield, phase_power_data, device_label)
    return {'total_power': total_power, 'daily_yield': daily_yield, **phase_data}


def collect_data(
    modbus_pool: ModbusClientPool,
    speedwire: SpeedwireCollector,
    device_state: DeviceCollectionState,
) -> Dict[str, Any]:
    """Collect readings from all configured devices in parallel."""
    data_collection: Dict[str, Any] = {}
    devices = list(SMA_DEVICES.items())

    if not devices:
        return data_collection

    read_timeout = _device_read_timeout_seconds()
    worker_count = max(1, len(devices))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures: Dict[Any, Tuple[str, str]] = {}
        for device_id, device_info in devices:
            device_label = f"device {device_id} ({device_info['name']})"
            if device_state.should_probe(device_id):
                future = executor.submit(
                    _collect_device_raw, device_id, device_info, modbus_pool, speedwire
                )
                futures[future] = (device_id, device_label)
            else:
                data_collection[device_id] = device_state.contribution_without_probe(device_id)

        for future in as_completed(futures):
            device_id, device_label = futures[future]
            try:
                fresh = future.result(timeout=read_timeout)
            except FuturesTimeoutError:
                logging.warning("%s: read timed out after %.1fs", device_label, read_timeout)
                fresh = None
            except Exception:
                logging.exception("Error collecting data for %s", device_label)
                fresh = None

            data_collection[device_id] = device_state.resolve_contribution(
                device_id, device_label, fresh
            )

    device_rows = list(data_collection.values())
    if not device_rows:
        return data_collection

    aggregate: Dict[str, int] = {key: sum_key(device_rows, key) for key in PHASE_METRIC_KEYS}
    aggregate['total_power'] = sum_key(device_rows, 'total_power')
    aggregate['daily_yield'] = sum_key(device_rows, 'daily_yield')
    data_collection['aggregate'] = aggregate
    return data_collection
