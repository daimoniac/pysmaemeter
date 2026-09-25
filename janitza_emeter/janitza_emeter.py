#!/usr/bin/env python3
"""Read Janitza UMG 604-PRO via Modbus TCP and emit an SMA Energy Meter grid telegram."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pymodbus.client.sync import ModbusTcpClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.emeter import emeterPacket
from lib.multicast import send_udp_multicast
from lib.sma_units import ENERGY_WH_TO_JOULE, POWER_SCALE

try:
    from mapping import (
        GridSnapshot,
        LastSnapshotHold,
        PhaseReading,
        REG_COUNT,
        REG_START,
        apply_invert,
        snapshot_from_registers,
    )
except ImportError:
    from janitza_emeter.mapping import (
        GridSnapshot,
        LastSnapshotHold,
        PhaseReading,
        REG_COUNT,
        REG_START,
        apply_invert,
        snapshot_from_registers,
    )

logging.getLogger('pymodbus').setLevel(logging.CRITICAL)

CONFIG_PATH = Path(__file__).resolve().parent / 'config.json'


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def validate_config(config: Dict[str, Any]) -> None:
    for section in ('logging', 'modbus', 'emeter', 'multicast', 'scheduler'):
        if section not in config or not isinstance(config[section], dict):
            raise ValueError(f"Missing config section {section}")
    modbus = config['modbus']
    for key in ('host', 'port', 'unit_id', 'timeout'):
        if key not in modbus:
            raise ValueError(f"Missing config key modbus.{key}")
    if 'serial_number' not in config['emeter']:
        raise ValueError("Missing config key emeter.serial_number")
    if 'invert_direction' not in config['emeter']:
        raise ValueError("Missing config key emeter.invert_direction")
    multicast = config['multicast']
    for key in ('address', 'port', 'ttl'):
        if key not in multicast:
            raise ValueError(f"Missing config key multicast.{key}")
    scheduler = config['scheduler']
    for key in ('interval_seconds', 'hold_ticks'):
        if key not in scheduler:
            raise ValueError(f"Missing config key scheduler.{key}")


def _scale_power(watts: float) -> int:
    return int(round(watts * POWER_SCALE))


def _scale_energy(watt_hours: float) -> int:
    return int(round(watt_hours * ENERGY_WH_TO_JOULE))


def _add_bidirectional_active(
    packet: emeterPacket, suffix: str, phase: PhaseReading
) -> None:
    packet.addMeasurementValue(
        getattr(emeterPacket, f'SMA_POSITIVE_ACTIVE_POWER{suffix}'),
        _scale_power(phase.import_w),
    )
    packet.addCounterValue(
        getattr(emeterPacket, f'SMA_POSITIVE_ACTIVE_ENERGY{suffix}'),
        _scale_energy(phase.import_wh),
    )
    packet.addMeasurementValue(
        getattr(emeterPacket, f'SMA_NEGATIVE_ACTIVE_POWER{suffix}'),
        _scale_power(phase.export_w),
    )
    packet.addCounterValue(
        getattr(emeterPacket, f'SMA_NEGATIVE_ACTIVE_ENERGY{suffix}'),
        _scale_energy(phase.export_wh),
    )


def build_packet(serial_number: int, snapshot: GridSnapshot) -> bytes:
    packet = emeterPacket(serial_number)
    packet.begin(int(time.time() * 1000), skip_phase_values=True)
    _add_bidirectional_active(packet, '', snapshot.total)
    _add_bidirectional_active(packet, '_L1', snapshot.l1)
    _add_bidirectional_active(packet, '_L2', snapshot.l2)
    _add_bidirectional_active(packet, '_L3', snapshot.l3)
    packet.end()
    return bytes(packet.getData()[:packet.getLength()])


class JanitzaModbus:
    def __init__(self, host: str, port: int, unit_id: int, timeout: float) -> None:
        self.host = host
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.timeout = float(timeout)
        self._client: Optional[ModbusTcpClient] = None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _connect(self) -> ModbusTcpClient:
        if self._client is not None:
            return self._client
        client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        if not client.connect():
            raise ConnectionError(f"Connection to {self.host}:{self.port} not possible")
        self._client = client
        return client

    def read_registers(self) -> List[int]:
        try:
            client = self._connect()
            result = client.read_holding_registers(REG_START, REG_COUNT, unit=self.unit_id)
            if result is None or result.isError():
                raise ConnectionError(f"Modbus read failed: {result}")
            registers = list(result.registers)
            if len(registers) < REG_COUNT:
                raise ConnectionError(
                    f"Modbus short read: expected {REG_COUNT}, got {len(registers)}"
                )
            return registers
        except Exception:
            self.close()
            raise


def _snapshot_log_line(snapshot: GridSnapshot, prefix: str = '') -> str:
    t = snapshot.total
    return (
        f"{prefix}"
        f"P+ {t.import_w:.0f}W P- {t.export_w:.0f}W | "
        f"E+ {t.import_wh:.0f}Wh E- {t.export_wh:.0f}Wh | "
        f"L1 {snapshot.l1.import_w:.0f}/{snapshot.l1.export_w:.0f}W "
        f"L2 {snapshot.l2.import_w:.0f}/{snapshot.l2.export_w:.0f}W "
        f"L3 {snapshot.l3.import_w:.0f}/{snapshot.l3.export_w:.0f}W"
    )


def tick(
    client: JanitzaModbus,
    hold: LastSnapshotHold,
    config: Dict[str, Any],
) -> None:
    invert = bool(config['emeter']['invert_direction'])
    fresh: Optional[GridSnapshot] = None
    try:
        registers = client.read_registers()
        fresh = apply_invert(snapshot_from_registers(registers), invert)
    except Exception as exc:
        logging.warning(f"Janitza read failed: {exc}")

    snapshot, miss_ticks, restored = hold.resolve_with_status(fresh)
    if restored:
        logging.info("Janitza reply restored")
    if snapshot is None:
        if miss_ticks == hold.hold_ticks + 1:
            logging.warning(
                f"Janitza stale after {hold.hold_ticks} ticks, stopping multicast until reply"
            )
        return
    if miss_ticks == 1:
        logging.info("Janitza no reply, repeating last snapshot")
    elif miss_ticks > 1:
        logging.debug(
            f"Janitza no reply, still repeating last snapshot ({miss_ticks}/{hold.hold_ticks})"
        )

    data = build_packet(int(config['emeter']['serial_number']), snapshot)
    multicast = config['multicast']
    send_udp_multicast(data, multicast['address'], multicast['port'], multicast['ttl'])
    logging.info(f"Sent: {_snapshot_log_line(snapshot)}")


def main() -> None:
    try:
        config = load_config()
        validate_config(config)
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
        logging.error(f"Configuration error: {exc}")
        sys.exit(1)

    logging_config = config.get('logging', {})
    logging.basicConfig(
        level=getattr(logging, str(logging_config.get('level', 'INFO')), logging.INFO),
        format=str(logging_config.get('format', '%(levelname)s - %(message)s')),
        handlers=[logging.StreamHandler()],
    )

    hold_ticks = int(config['scheduler']['hold_ticks'])
    interval = float(config['scheduler']['interval_seconds'])
    modbus = config['modbus']
    client = JanitzaModbus(
        host=str(modbus['host']),
        port=int(modbus['port']),
        unit_id=int(modbus['unit_id']),
        timeout=float(modbus['timeout']),
    )
    hold = LastSnapshotHold(hold_ticks=hold_ticks)

    logging.info(
        "Starting Janitza SMA grid meter emulator "
        f"(serial {config['emeter']['serial_number']}, invert={config['emeter']['invert_direction']})"
    )

    try:
        while True:
            started = time.monotonic()
            try:
                tick(client, hold, config)
            except Exception as exc:
                logging.error(f"Error in tick: {exc}")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        logging.info("Program terminated by user")
    finally:
        client.close()


if __name__ == '__main__':
    main()
