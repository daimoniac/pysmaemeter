import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sma_emeter.phase_data import extract_phase_data, sum_key


class TestPhaseData(unittest.TestCase):
    def test_zero_phase_power_not_replaced(self):
        phase = extract_phase_data(
            total_power=3000,
            daily_yield=100,
            phase_power_data={'p1_power': 0, 'p2_power': 1500, 'p3_power': 1500},
        )
        self.assertEqual(phase['p1_power'], 0)

    def test_aggregate_total_power_from_devices(self):
        rows = [
            {'total_power': 1000, 'p1_power': 400, 'p2_power': 300, 'p3_power': 200},
            {'total_power': 500, 'p1_power': 100, 'p2_power': 200, 'p3_power': 100},
        ]
        self.assertEqual(sum_key(rows, 'total_power'), 1500)


if __name__ == '__main__':
    unittest.main()
