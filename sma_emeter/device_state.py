import logging
from typing import Any, Dict, Optional

STALE_DEVICE_TICK_LIMIT = 5
PROBE_INTERVAL_AFTER_STALE = 30


def zero_device_snapshot() -> Dict[str, int]:
    return {
        'total_power': 0,
        'daily_yield': 0,
        'p1_power': 0,
        'p1_yield': 0,
        'p2_power': 0,
        'p2_yield': 0,
        'p3_power': 0,
        'p3_yield': 0,
    }


class DeviceCollectionState:
    """Tracks last-known device readings and consecutive miss counts."""

    def __init__(self) -> None:
        self._devices: Dict[str, Dict[str, Any]] = {}

    def should_probe(self, device_id: str) -> bool:
        """False when a long-dead device can skip I/O until the next probe interval."""
        entry = self._devices.get(device_id, {})
        miss_ticks = int(entry.get('miss_ticks', 0))
        if miss_ticks <= STALE_DEVICE_TICK_LIMIT:
            return True
        return miss_ticks % PROBE_INTERVAL_AFTER_STALE == 0

    def contribution_without_probe(self, device_id: str) -> Dict[str, int]:
        """Return last stale contribution or zero without incrementing miss count."""
        entry = self._devices.get(device_id, {})
        last_known: Optional[Dict[str, int]] = entry.get('last_known')
        miss_ticks = int(entry.get('miss_ticks', 0))
        if last_known is not None and miss_ticks <= STALE_DEVICE_TICK_LIMIT:
            return last_known
        return zero_device_snapshot()

    def resolve_contribution(
        self, device_id: str, device_label: str, fresh: Optional[Dict[str, int]]
    ) -> Dict[str, int]:
        """Return fresh data, last-known for up to STALE_DEVICE_TICK_LIMIT misses, or zeros."""
        entry = self._devices.get(device_id, {})
        last_known: Optional[Dict[str, int]] = entry.get('last_known')
        miss_ticks = int(entry.get('miss_ticks', 0))

        if fresh is not None:
            self._devices[device_id] = {'last_known': fresh, 'miss_ticks': 0}
            return fresh

        miss_ticks += 1
        if last_known is not None and miss_ticks <= STALE_DEVICE_TICK_LIMIT:
            logging.info(
                "%s: no reply, using last known power (%d/%d)",
                device_label,
                miss_ticks,
                STALE_DEVICE_TICK_LIMIT,
            )
            self._devices[device_id] = {'last_known': last_known, 'miss_ticks': miss_ticks}
            return last_known

        if last_known is not None:
            logging.warning(
                "%s: no reply, contributing 0 (miss %d, stale limit %d)",
                device_label,
                miss_ticks,
                STALE_DEVICE_TICK_LIMIT,
            )
        else:
            logging.warning("%s: no reply and no last known value, contributing 0", device_label)

        self._devices[device_id] = {'last_known': last_known, 'miss_ticks': miss_ticks}
        return zero_device_snapshot()
