"""Janitza UMG 604-PRO holding registers -> SMA bidirectional grid channels."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

# First register of the contiguous read (see device modbus.csv).
REG_START = 19020
REG_COUNT = 58  # 19020 .. 19077 (last float starts at 19076)

ADDR_P_L1 = 19020
ADDR_P_L2 = 19022
ADDR_P_L3 = 19024
ADDR_P_SUM = 19026
ADDR_WH_V_L1 = 19062
ADDR_WH_V_L2 = 19064
ADDR_WH_V_L3 = 19066
ADDR_WH_Z_L1 = 19070
ADDR_WH_Z_L2 = 19072
ADDR_WH_Z_L3 = 19074
ADDR_WH_Z_SUM = 19076


@dataclass(frozen=True)
class PhaseReading:
    import_w: float
    export_w: float
    import_wh: float
    export_wh: float


@dataclass(frozen=True)
class GridSnapshot:
    total: PhaseReading
    l1: PhaseReading
    l2: PhaseReading
    l3: PhaseReading


def _require_finite(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} is not a finite number: {value!r}")
    return value


def decode_float_be(registers: List[int], address: int) -> float:
    """Decode an IEEE-754 float from two big-endian holding registers."""
    offset = address - REG_START
    if offset < 0 or offset + 1 >= len(registers):
        raise ValueError(f"register {address} not in read buffer")
    raw = struct.pack('>HH', registers[offset] & 0xFFFF, registers[offset + 1] & 0xFFFF)
    return struct.unpack('>f', raw)[0]


def split_signed_power(watts: float) -> Tuple[float, float]:
    """Split a signed Janitza watt value into SMA import (+) and export (-)."""
    if watts >= 0:
        return (watts, 0.0)
    return (0.0, -watts)


def _phase_from_signed(power_w: float, import_wh: float, export_wh: float) -> PhaseReading:
    import_w, export_w = split_signed_power(power_w)
    return PhaseReading(
        import_w=_require_finite('import_w', import_w),
        export_w=_require_finite('export_w', export_w),
        import_wh=_require_finite('import_wh', max(0.0, import_wh)),
        export_wh=_require_finite('export_wh', max(0.0, export_wh)),
    )


def snapshot_from_registers(registers: List[int]) -> GridSnapshot:
    """Build a grid snapshot from a holding-register block starting at 19020."""
    if len(registers) < REG_COUNT:
        raise ValueError(f"expected {REG_COUNT} registers, got {len(registers)}")

    p1 = decode_float_be(registers, ADDR_P_L1)
    p2 = decode_float_be(registers, ADDR_P_L2)
    p3 = decode_float_be(registers, ADDR_P_L3)
    p_sum = decode_float_be(registers, ADDR_P_SUM)
    wh_v1 = decode_float_be(registers, ADDR_WH_V_L1)
    wh_v2 = decode_float_be(registers, ADDR_WH_V_L2)
    wh_v3 = decode_float_be(registers, ADDR_WH_V_L3)
    wh_z1 = decode_float_be(registers, ADDR_WH_Z_L1)
    wh_z2 = decode_float_be(registers, ADDR_WH_Z_L2)
    wh_z3 = decode_float_be(registers, ADDR_WH_Z_L3)
    wh_z_sum = decode_float_be(registers, ADDR_WH_Z_SUM)

    l1 = _phase_from_signed(p1, wh_v1, wh_z1)
    l2 = _phase_from_signed(p2, wh_v2, wh_z2)
    l3 = _phase_from_signed(p3, wh_v3, wh_z3)
    # Import total is the sum of phase counters (HT-only sum is a tariff split).
    import_wh_sum = l1.import_wh + l2.import_wh + l3.import_wh
    total = _phase_from_signed(p_sum, import_wh_sum, wh_z_sum)
    return GridSnapshot(total=total, l1=l1, l2=l2, l3=l3)


def _swap_direction(phase: PhaseReading) -> PhaseReading:
    return PhaseReading(
        import_w=phase.export_w,
        export_w=phase.import_w,
        import_wh=phase.export_wh,
        export_wh=phase.import_wh,
    )


def apply_invert(snapshot: GridSnapshot, invert_direction: bool) -> GridSnapshot:
    """Swap SMA import/export on all channels when CT orientation is reversed."""
    if not invert_direction:
        return snapshot
    return GridSnapshot(
        total=_swap_direction(snapshot.total),
        l1=_swap_direction(snapshot.l1),
        l2=_swap_direction(snapshot.l2),
        l3=_swap_direction(snapshot.l3),
    )


class LastSnapshotHold:
    """Repeat the last good snapshot for a few ticks, then return None (silence)."""

    def __init__(self, hold_ticks: int = 5) -> None:
        if hold_ticks < 1:
            raise ValueError("hold_ticks must be >= 1")
        self.hold_ticks = hold_ticks
        self._last: Optional[GridSnapshot] = None
        self._miss_ticks = 0

    def resolve_with_status(
        self, fresh: Optional[GridSnapshot]
    ) -> Tuple[Optional[GridSnapshot], int, bool]:
        """Return (snapshot_or_None, miss_ticks, restored).

        miss_ticks is 0 on a fresh read. After hold_ticks misses, snapshot is None.
        """
        if fresh is not None:
            restored = self._miss_ticks > 0
            self._last = fresh
            self._miss_ticks = 0
            return fresh, 0, restored

        self._miss_ticks += 1
        if self._last is None or self._miss_ticks > self.hold_ticks:
            return None, self._miss_ticks, False
        return self._last, self._miss_ticks, False
