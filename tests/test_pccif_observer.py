import logging
import unittest
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph


class PCCIFObserverTests(unittest.TestCase):
    def device(self):
        device = object.__new__(PCCIF_Periph)
        device.mem = [0] * 4096
        device.rchnum = 0x11
        device.pccifid = 0
        device.log = logging.getLogger("pccif-observer-test")
        return device

    def test_passive_ack_and_read_counts_default_off(self):
        device = self.device()
        self.assertEqual(device.hw_read(0x10, 4), 0x11)
        self.assertFalse(hasattr(device, "register_observer"))
        device.enable_control_observer()
        self.assertTrue(device.hw_write(0x14, 4, 1))
        self.assertEqual(device.hw_read(0x10, 4), 0x10)
        before = (list(device.mem), device.rchnum)
        device.enable_control_observer()
        facts = device.control_observation()
        self.assertEqual(facts["events"], 2)
        self.assertEqual(facts["registers"][0]["last_read"], 0x10)
        self.assertEqual((device.mem, device.rchnum), before)

    def test_sram_payloads_are_excluded_and_failed_reads_are_not_recorded(self):
        device = self.device()
        device.enable_control_observer()
        device.hw_write(0x100, 4, 0x12345678)
        self.assertEqual(device.hw_read(0x100, 4), 0x12345678)
        with self.assertRaises(AssertionError): device.hw_read(0x30, 4)
        self.assertEqual(device.control_observation()["events"], 0)
        self.assertEqual(device.control_observation()["rejected"], 0)
