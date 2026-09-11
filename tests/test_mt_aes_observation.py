"""Trace confidentiality, bounds and transparency without native dependencies."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("aes_observation", Path(__file__).resolve().parents[1] /
    "firmwire/vendor/mtk/hw/aes_observation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AESObservationTests(unittest.TestCase):
    def test_keys_payload_iv_and_outputs_are_not_recorded(self):
        trace = module.AESControlTrace()
        for offset in list(range(0x10, 0x8c, 4)) + [0x100, 1]:
            trace.record("write", offset, 4, 0xdeadbeef)
            trace.record("read", offset, 4, 0xfeedface)
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["first_controls"], [])
        self.assertEqual(snapshot["recent_controls"], [])
        self.assertNotIn(str(0xdeadbeef), str(snapshot))
        self.assertFalse(snapshot["key_and_payload_values_recorded"])

    def test_control_values_and_counts_are_bounded(self):
        trace = module.AESControlTrace()
        for i in range(1000):
            trace.record("write", 8, 4, i)
            trace.record("write", 10000 + i, 1, i)
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["events"], 2000)
        self.assertEqual(len(snapshot["first_controls"]), 32)
        self.assertEqual(len(snapshot["recent_controls"]), 32)
        self.assertEqual(len(snapshot["access_counts"]), 2)

    def test_snapshots_are_independent_and_polling_collapses(self):
        trace = module.AESControlTrace()
        for _ in range(100):
            trace.record("read", 8, 4, 0x8000)
        snapshot = trace.snapshot()
        self.assertEqual(len(snapshot["recent_controls"]), 1)
        self.assertEqual(snapshot["recent_controls"][0]["last_sequence"], 100)
        snapshot["access_counts"]["0x8"]["read"] = 0
        self.assertEqual(trace.snapshot()["access_counts"]["0x8"]["read"], 100)

    def test_partial_controls_do_not_capture_neighbor_bytes(self):
        trace = module.AESControlTrace()
        trace.record("read", 12, 8, 0xfeedface12345678)
        self.assertEqual(trace.snapshot()["first_controls"], [])

    @unittest.skipUnless(importlib.util.find_spec("avatar2"), "requires FirmWire runtime dependencies")
    def test_observer_preserves_real_peripheral_responses_and_storage(self):
        from firmwire.emulator.firmwire import FirmWireEmu
        from firmwire.vendor.mtk.hw.AESPeripheral import AES_TOP0_Periph

        plain = AES_TOP0_Periph("plain", 0, 0x1000, firmwire_machine=Mock(spec=FirmWireEmu))
        observed = AES_TOP0_Periph("observed", 0, 0x1000, firmwire_machine=Mock(spec=FirmWireEmu))
        self.assertIsNone(plain.control_observation())
        observed.enable_control_observer()
        for offset, size, value in [(0, 4, 0), (4, 4, 0x12), (12, 4, 0x10),
                                    (0x10, 4, 0xdeadbeef), (0x20, 4, 0xfeedface),
                                    (0x40, 4, 0x12345678), (0x50, 4, 0xabcdef01),
                                    (8, 4, 1), (8, 4, 8), (8, 4, 2), (8, 4, 0)]:
            self.assertEqual(plain.hw_write(offset, size, value), observed.hw_write(offset, size, value))
            self.assertEqual(plain.hw_read(offset, size), observed.hw_read(offset, size))
            self.assertEqual(plain.mem, observed.mem)
        self.assertIsNone(plain.control_observation())
        self.assertGreater(observed.control_observation()["events"], 0)
