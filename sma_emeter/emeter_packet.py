import logging
import socket
import time
from typing import Dict, Tuple

from lib.emeter import emeterPacket

from sma_emeter.config import CONFIG
from sma_emeter.types import EmeterPayload

POWER_SCALE = 10
ENERGY_SCALE = 3600

_PHASE_CONSTS: Dict[str, Tuple[int, int, int, int]] = {
    '': (
        emeterPacket.SMA_POSITIVE_ACTIVE_POWER,
        emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY,
        emeterPacket.SMA_NEGATIVE_ACTIVE_POWER,
        emeterPacket.SMA_NEGATIVE_ACTIVE_ENERGY,
    ),
    '_L1': (
        emeterPacket.SMA_POSITIVE_ACTIVE_POWER_L1,
        emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY_L1,
        emeterPacket.SMA_NEGATIVE_ACTIVE_POWER_L1,
        emeterPacket.SMA_NEGATIVE_ACTIVE_ENERGY_L1,
    ),
    '_L2': (
        emeterPacket.SMA_POSITIVE_ACTIVE_POWER_L2,
        emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY_L2,
        emeterPacket.SMA_NEGATIVE_ACTIVE_POWER_L2,
        emeterPacket.SMA_NEGATIVE_ACTIVE_ENERGY_L2,
    ),
    '_L3': (
        emeterPacket.SMA_POSITIVE_ACTIVE_POWER_L3,
        emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY_L3,
        emeterPacket.SMA_NEGATIVE_ACTIVE_POWER_L3,
        emeterPacket.SMA_NEGATIVE_ACTIVE_ENERGY_L3,
    ),
}


def add_phase_measurements(
    packet: emeterPacket, phase_suffix: str, power: int, energy_wh: int
) -> None:
    pos_power, pos_energy, neg_power, neg_energy = _PHASE_CONSTS[phase_suffix]
    packet.addMeasurementValue(pos_power, 0)
    packet.addCounterValue(pos_energy, 0)
    packet.addMeasurementValue(neg_power, int(power * POWER_SCALE))
    packet.addCounterValue(neg_energy, int(energy_wh * ENERGY_SCALE))


def build_emeter_packet(payload: EmeterPayload) -> bytes:
    packet = emeterPacket(CONFIG['emeter']['serial_number'])
    packet.begin(int(time.time() * 1000), skip_phase_values=True)

    lifetime_wh = payload.lifetime_energy_wh()
    add_phase_measurements(packet, '', payload.power, lifetime_wh)
    add_phase_measurements(packet, '_L1', payload.p1_power, payload.p1_yield)
    add_phase_measurements(packet, '_L2', payload.p2_power, payload.p2_yield)
    add_phase_measurements(packet, '_L3', payload.p3_power, payload.p3_yield)

    packet.end()
    return bytes(packet.getData()[:packet.getLength()])


class MulticastSender:
    """Reusable UDP multicast socket for emeter packets."""

    def __init__(self) -> None:
        multicast = CONFIG['multicast']
        self._destination = (multicast['address'], multicast['port'])
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast['ttl'])

    def send(self, data: bytes, log_msg: str) -> None:
        self._sock.sendto(data, self._destination)
        logging.info("Sent: %s", log_msg)

    def close(self) -> None:
        self._sock.close()


def send_emeter_packet(sender: MulticastSender, payload: EmeterPayload) -> None:
    data = build_emeter_packet(payload)
    lifetime_kwh = payload.total_negative_active_energy_kwh
    sender.send(
        data,
        (
            f"{payload.log_prefix}"
            f"current power: {payload.power}W | Daily total: {payload.energy}Wh | "
            f"lifetime total: {lifetime_kwh}kWh | "
            f"L1: {payload.p1_power}W/{payload.p1_yield}Wh | "
            f"L2: {payload.p2_power}W/{payload.p2_yield}Wh | "
            f"L3: {payload.p3_power}W/{payload.p3_yield}Wh"
        ),
    )
