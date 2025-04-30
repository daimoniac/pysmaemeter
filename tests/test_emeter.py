import unittest
import time
import sys
import os

# Add the parent directory to sys.path so emeter can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from emeter import emeterPacket

class TestEmeterPacket(unittest.TestCase):
    def setUp(self):
        self.serial_number = 12345678
        self.packet = emeterPacket(self.serial_number)

    def test_serial_number_in_packet(self):
        # Serial number is stored in the header
        data = self.packet.getData()
        # Serial number is 4 bytes, big-endian, at offset found by initEmeterPacket
        serial_bytes = self.serial_number.to_bytes(4, 'big')
        self.assertIn(serial_bytes, data[:32])

    def test_begin_and_end_length(self):
        self.packet.begin(int(time.time() * 1000))
        length_before = self.packet.getLength()
        self.packet.end()
        length_after = self.packet.getLength()
        self.assertGreater(length_after, length_before)

    def test_add_measurement_value(self):
        self.packet.begin(int(time.time() * 1000))
        old_length = self.packet.getLength()
        self.packet.addMeasurementValue(emeterPacket.SMA_POSITIVE_ACTIVE_POWER, 1234)
        self.assertEqual(self.packet.getLength(), old_length + 8)

    def test_add_counter_value(self):
        self.packet.begin(int(time.time() * 1000))
        old_length = self.packet.getLength()
        self.packet.addCounterValue(emeterPacket.SMA_POSITIVE_ACTIVE_ENERGY, 567890)
        self.assertEqual(self.packet.getLength(), old_length + 12)

    def test_packet_data_type(self):
        self.assertIsInstance(self.packet.getData(), bytearray)

    def test_storeU16BE(self):
        pos = 0
        value = 0x1234
        self.packet.storeU16BE(pos, value)
        data = self.packet.getData()
        self.assertEqual(data[pos], 0x12)
        self.assertEqual(data[pos+1], 0x34)

    def test_storeU32BE(self):
        pos = 0
        value = 0x12345678
        self.packet.storeU32BE(pos, value)
        data = self.packet.getData()
        self.assertEqual(data[pos], 0x12)
        self.assertEqual(data[pos+1], 0x34)
        self.assertEqual(data[pos+2], 0x56)
        self.assertEqual(data[pos+3], 0x78)

    def test_storeU64BE(self):
        pos = 0
        value = 0x1234567890ABCDEF
        self.packet.storeU64BE(pos, value)
        data = self.packet.getData()
        self.assertEqual(data[pos], 0x12)
        self.assertEqual(data[pos+1], 0x34)
        self.assertEqual(data[pos+2], 0x56)
        self.assertEqual(data[pos+3], 0x78)
        self.assertEqual(data[pos+4], 0x90)
        self.assertEqual(data[pos+5], 0xAB)
        self.assertEqual(data[pos+6], 0xCD)
        self.assertEqual(data[pos+7], 0xEF)

if __name__ == '__main__':
    unittest.main()