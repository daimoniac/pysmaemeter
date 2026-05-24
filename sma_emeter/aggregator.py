import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional

from sma_emeter.config import (
    MODBUS_DEVICE_TYPE,
    PHASE_METRIC_KEYS,
    SMA_DEVICES,
    SPEEDWIRE_DEVICE_TYPE,
)
from sma_emeter.modbus_reader import ModbusClientPool
from sma_emeter.phase_data import extract_phase_data, sum_key
from sma_emeter.speedwire import SpeedwireCollector
from sma_emeter.state import sanitize_daily_yield


def _collect_one_device(
    device_id: str,
    device_info: Dict[str, Any],
    modbus_pool: ModbusClientPool,
    speedwire: SpeedwireCollector,
) -> Optional[Dict[str, int]]:
    device_label = f"device {device_id} ({device_info['name']})"
    device_type = device_info['type']

    if device_type == MODBUS_DEVICE_TYPE:
        data = modbus_pool.read_device(device_id, device_type)
        if data is None:
            logging.warning("No data from %s", device_label)
            return None
        total_power = data['total_power']
        daily_yield = sanitize_daily_yield(data['daily_yield'], device_label)
        phase_power_data = {k: data[k] for k in ('p1_power', 'p2_power', 'p3_power')}

    elif device_type == SPEEDWIRE_DEVICE_TYPE:
        data = speedwire.fetch()
        total_power = data.get('spotacpower', 0)
        daily_yield = sanitize_daily_yield(data.get('tagesertrag', 0), device_label)
        phase_power_data = data
    else:
        return None

    phase_data = extract_phase_data(total_power, daily_yield, phase_power_data, device_label)
    return {'total_power': total_power, 'daily_yield': daily_yield, **phase_data}


def collect_data(
    modbus_pool: ModbusClientPool, speedwire: SpeedwireCollector
) -> Dict[str, Any]:
    """Collect readings from all configured devices in parallel."""
    data_collection: Dict[str, Any] = {}
    devices = list(SMA_DEVICES.items())

    if not devices:
        return data_collection

    with ThreadPoolExecutor(max_workers=len(devices)) as executor:
        futures = {
            executor.submit(
                _collect_one_device, device_id, device_info, modbus_pool, speedwire
            ): device_id
            for device_id, device_info in devices
        }
        for future in as_completed(futures):
            device_id = futures[future]
            try:
                result = future.result()
                if result is not None:
                    data_collection[device_id] = result
            except Exception:
                logging.exception("Error collecting data for device %s", device_id)

    device_rows = list(data_collection.values())
    if not device_rows:
        return data_collection

    aggregate: Dict[str, int] = {key: sum_key(device_rows, key) for key in PHASE_METRIC_KEYS}
    aggregate['total_power'] = sum_key(device_rows, 'total_power')
    aggregate['daily_yield'] = sum_key(device_rows, 'daily_yield')
    data_collection['aggregate'] = aggregate
    return data_collection
