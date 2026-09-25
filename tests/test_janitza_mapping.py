import math
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'janitza_emeter'))

from mapping import (
    ADDR_P_L1,
    ADDR_P_SUM,
    ADDR_WH_V_L1,
    ADDR_WH_Z_L1,
    ADDR_WH_Z_SUM,
    LastSnapshotHold,
    REG_COUNT,
    REG_START,
    apply_invert,
    decode_float_be,
    snapshot_from_registers,
    split_signed_power,
)


def _encode_float(value: float) -> tuple:
    raw = struct.pack('>f', value)
    hi, lo = struct.unpack('>HH', raw)
    return hi, lo


def _blank_registers():
    return [0] * REG_COUNT


def _put_float(regs, address, value):
    offset = address - REG_START
    hi, lo = _encode_float(value)
    regs[offset] = hi
    regs[offset + 1] = lo


class TestJanitzaMapping(unittest.TestCase):
    def test_split_signed_power(self):
        self.assertEqual(split_signed_power(1500.0), (1500.0, 0.0))
        self.assertEqual(split_signed_power(-800.0), (0.0, 800.0))
        self.assertEqual(split_signed_power(0.0), (0.0, 0.0))

    def test_decode_float_be(self):
        regs = _blank_registers()
        _put_float(regs, ADDR_P_L1, -1234.5)
        self.assertAlmostEqual(decode_float_be(regs, ADDR_P_L1), -1234.5, places=3)

    def test_snapshot_import_and_export(self):
        regs = _blank_registers()
        _put_float(regs, ADDR_P_L1, 100.0)
        _put_float(regs, 19022, -40.0)
        _put_float(regs, 19024, 10.0)
        _put_float(regs, ADDR_P_SUM, 70.0)
        _put_float(regs, ADDR_WH_V_L1, 1.0)
        _put_float(regs, 19064, 2.0)
        _put_float(regs, 19066, 3.0)
        _put_float(regs, ADDR_WH_Z_L1, 10.0)
        _put_float(regs, 19072, 20.0)
        _put_float(regs, 19074, 30.0)
        _put_float(regs, ADDR_WH_Z_SUM, 60.0)

        snap = snapshot_from_registers(regs)
        self.assertEqual(snap.l1.import_w, 100.0)
        self.assertEqual(snap.l1.export_w, 0.0)
        self.assertEqual(snap.l2.import_w, 0.0)
        self.assertEqual(snap.l2.export_w, 40.0)
        self.assertEqual(snap.total.import_w, 70.0)
        self.assertEqual(snap.total.export_w, 0.0)
        self.assertEqual(snap.total.import_wh, 6.0)
        self.assertEqual(snap.total.export_wh, 60.0)

    def test_invert_swaps_all_channels(self):
        regs = _blank_registers()
        _put_float(regs, ADDR_P_SUM, -2500.0)
        _put_float(regs, ADDR_P_L1, -1000.0)
        _put_float(regs, 19022, -800.0)
        _put_float(regs, 19024, -700.0)
        _put_float(regs, ADDR_WH_V_L1, 1)
        _put_float(regs, 19064, 1)
        _put_float(regs, 19066, 1)
        _put_float(regs, ADDR_WH_Z_L1, 4)
        _put_float(regs, 19072, 5)
        _put_float(regs, 19074, 6)
        _put_float(regs, ADDR_WH_Z_SUM, 15)
        snap = apply_invert(snapshot_from_registers(regs), True)
        self.assertEqual(snap.total.import_w, 2500.0)
        self.assertEqual(snap.total.export_w, 0.0)
        self.assertEqual(snap.total.import_wh, 15.0)
        self.assertEqual(snap.total.export_wh, 3.0)

    def test_hold_then_silence(self):
        hold = LastSnapshotHold(hold_ticks=2)
        regs = _blank_registers()
        _put_float(regs, ADDR_P_SUM, 1.0)
        _put_float(regs, ADDR_WH_Z_SUM, 0.0)
        fresh = snapshot_from_registers(regs)
        snap, miss, restored = hold.resolve_with_status(fresh)
        self.assertIs(snap, fresh)
        self.assertEqual(miss, 0)
        self.assertFalse(restored)

        snap, miss, restored = hold.resolve_with_status(None)
        self.assertIsNotNone(snap)
        self.assertEqual(miss, 1)
        snap, miss, restored = hold.resolve_with_status(None)
        self.assertEqual(miss, 2)
        self.assertIsNotNone(snap)
        snap, miss, restored = hold.resolve_with_status(None)
        self.assertIsNone(snap)
        self.assertEqual(miss, 3)

        snap, miss, restored = hold.resolve_with_status(fresh)
        self.assertTrue(restored)
        self.assertEqual(miss, 0)

    def test_nan_rejected(self):
        regs = _blank_registers()
        _put_float(regs, ADDR_P_SUM, float('nan'))
        with self.assertRaises(ValueError):
            snapshot_from_registers(regs)
        self.assertTrue(math.isnan(float('nan')))


if __name__ == '__main__':
    unittest.main()
