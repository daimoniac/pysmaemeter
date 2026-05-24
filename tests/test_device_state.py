import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sma_emeter.device_state import DeviceCollectionState, STALE_DEVICE_TICK_LIMIT


class TestDeviceCollectionState(unittest.TestCase):
    def setUp(self):
        self.state = DeviceCollectionState()
        self.fresh = {
            'total_power': 2500,
            'daily_yield': 100,
            'p1_power': 800,
            'p1_yield': 30,
            'p2_power': 900,
            'p2_yield': 35,
            'p3_power': 800,
            'p3_yield': 35,
        }

    def test_uses_last_known_for_five_misses(self):
        label = 'device 189 (Garage)'
        self.state.resolve_contribution('189', label, self.fresh)
        for miss in range(1, STALE_DEVICE_TICK_LIMIT + 1):
            result = self.state.resolve_contribution('189', label, None)
            self.assertEqual(result['total_power'], 2500, f'miss {miss}')
        result = self.state.resolve_contribution('189', label, None)
        self.assertEqual(result['total_power'], 0)

    def test_recovery_resets_miss_count(self):
        label = 'device 189 (Garage)'
        self.state.resolve_contribution('189', label, self.fresh)
        self.state.resolve_contribution('189', label, None)
        recovered = dict(self.fresh)
        recovered['total_power'] = 2600
        result = self.state.resolve_contribution('189', label, recovered)
        self.assertEqual(result['total_power'], 2600)
        result = self.state.resolve_contribution('189', label, None)
        self.assertEqual(result['total_power'], 2600)


if __name__ == '__main__':
    unittest.main()
