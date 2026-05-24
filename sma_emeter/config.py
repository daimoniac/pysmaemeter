import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / 'config.json'
TOTAL_COUNTER_PATH = PROJECT_ROOT / 'total_counter.json'

MODBUS_DEVICE_TYPE = 8001
SPEEDWIRE_DEVICE_TYPE = 9999

# Modbus holding registers for SMA type 8001 (address, field name).
MODBUS_REGISTERS_8001: List[Tuple[str, int]] = [
    ('_reserved_1', 30773),
    ('_reserved_2', 30961),
    ('total_power', 30775),
    ('daily_yield', 30535),
    ('p1_power', 30777),
    ('p2_power', 30779),
    ('p3_power', 30781),
]

INVALID_DAILY_YIELD_VALUES = {65535}

PHASE_METRIC_KEYS = [f'p{n}_{metric}' for n in (1, 2, 3) for metric in ('power', 'yield')]


class SnoozeWindow:
    """Pre-parsed snooze interval in minutes since midnight."""

    __slots__ = ('start_minutes', 'end_minutes')

    def __init__(self, start_minutes: int, end_minutes: int) -> None:
        self.start_minutes = start_minutes
        self.end_minutes = end_minutes

    def is_active(self) -> bool:
        now = time.localtime()
        current_minutes = now.tm_hour * 60 + now.tm_min
        if self.start_minutes <= self.end_minutes:
            return self.start_minutes <= current_minutes < self.end_minutes
        return current_minutes >= self.start_minutes or current_minutes < self.end_minutes


def _parse_snooze_window(snooze_config: Optional[Dict[str, str]]) -> Optional[SnoozeWindow]:
    if not snooze_config:
        return None
    try:
        start_h, start_m = map(int, snooze_config['start'].split(':'))
        end_h, end_m = map(int, snooze_config['end'].split(':'))
    except (KeyError, ValueError):
        logging.warning("Invalid snooze configuration; snooze disabled.")
        return None
    return SnoozeWindow(start_h * 60 + start_m, end_h * 60 + end_m)


def _configure_logging(config: Dict[str, Any]) -> None:
    logging_config = config.get('logging', {})
    level_name = logging_config.get('level', 'INFO')
    level = getattr(logging, level_name, logging.INFO)
    log_format = logging_config.get('format', '%(levelname)s - %(message)s')
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=[logging.StreamHandler()],
        force=True,
    )


def _validate_configuration(config: Dict[str, Any]) -> None:
    devices = config.get('devices')
    if not isinstance(devices, dict):
        raise ValueError("Configuration: 'devices' section missing or invalid")

    required_device_fields = {'type', 'name'}
    for device_id, device_info in devices.items():
        if not isinstance(device_info, dict):
            raise ValueError(f"Device {device_id}: configuration must be a dictionary")
        missing_fields = required_device_fields - set(device_info.keys())
        if missing_fields:
            raise ValueError(f"Device {device_id}: missing required fields {missing_fields}")
        if device_info['type'] not in (MODBUS_DEVICE_TYPE, SPEEDWIRE_DEVICE_TYPE):
            raise ValueError(f"Device {device_id}: unsupported device type {device_info['type']}")

    emeter_config = config.get('emeter')
    if not isinstance(emeter_config, dict):
        raise ValueError("Configuration: 'emeter' section missing or invalid")
    if 'totalyieldbaseline' not in emeter_config:
        raise ValueError("Configuration: missing emeter.totalyieldbaseline (kWh)")

    logging.info("Configuration validated: %d devices configured", len(devices))


def load_config() -> Dict[str, Any]:
    """Load, validate, and enrich configuration from config.json."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logging.error("Configuration file not found: %s", CONFIG_PATH)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error("Invalid JSON in config file: %s", e)
        sys.exit(1)

    _configure_logging(config)
    logging.info("Configuration loaded from %s", CONFIG_PATH)

    _validate_configuration(config)
    config['snooze_window'] = _parse_snooze_window(config.get('snooze'))
    return config


CONFIG: Dict[str, Any] = load_config()
SMA_DEVICES: Dict[str, Dict[str, Any]] = CONFIG['devices']
SNOOZE_WINDOW: Optional[SnoozeWindow] = CONFIG.get('snooze_window')
