import unittest

from firmwire.hw.register_observer import RegisterAccessObserver
from firmwire.vendor.mtk.hw.MDCPeripheral import MDCIRQ_Periph


class RegisterObserverTests(unittest.TestCase):
    def test_widths_are_distinct_and_snapshots_are_detached(self):
        observer = RegisterAccessObserver(32)
        observer.record("write", 4, 4, 0x1234)
        observer.record("read", 4, 4, 0x5678)
        observer.record("read", 4, 1, 0x78)
        snapshot = observer.snapshot()
        self.assertEqual(len(snapshot["registers"]), 2)
        word = snapshot["registers"][1]
        self.assertEqual((word["reads"], word["writes"], word["last_write"]), (1, 1, 0x1234))
        snapshot["registers"][1]["reads"] = 100
        snapshot["first"][0]["value"] = 999
        self.assertEqual(observer.snapshot()["registers"][1]["reads"], 1)
        self.assertEqual(observer.snapshot()["first"][0]["value"], 0x1234)

    def test_all_storage_is_bounded_and_drops_are_explicit(self):
        observer = RegisterAccessObserver(128, max_registers=2, history=3)
        for offset in range(20):
            observer.record("write", offset, 1, offset)
        facts = observer.snapshot()
        self.assertEqual((facts["events"], facts["untracked"]), (20, 18))
        self.assertEqual(len(facts["registers"]), 2)
        self.assertEqual(len(facts["first"]), 3)
        self.assertEqual(len(facts["recent"]), 3)
        self.assertEqual(facts["recent"][-1]["sequence"], 20)

    def test_invalid_accesses_never_enter_evidence(self):
        observer = RegisterAccessObserver(16)
        for args in (("execute", 0, 4, 0), ("read", -1, 1, 0),
                     ("write", 14, 4, 0), ("read", 0, 3, 0),
                     ("read", 0, 1, 256), ("read", True, 1, 0),
                     ("write", 0, 4, -1)):
            observer.record(*args)
        self.assertEqual(observer.snapshot()["rejected"], 7)
        self.assertEqual(observer.snapshot()["events"], 0)
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError): RegisterAccessObserver(value)

    def test_device_is_passive_default_off_and_enable_is_idempotent(self):
        device = object.__new__(MDCIRQ_Periph)
        device.mem = [0] * 4096
        device.hw_write(0x40, 4, 7)
        self.assertFalse(hasattr(device, "register_observer"))
        device.enable_control_observer()
        for offset, width, value in ((0x40, 4, 3), (0x501, 1, 5), (0x602, 2, 9)):
            self.assertTrue(device.hw_write(offset, width, value))
            self.assertEqual(device.hw_read(offset, width), value)
        before = list(device.mem)
        device.enable_control_observer()
        for _ in range(3):
            self.assertEqual(device.control_observation()["events"], 6)
        self.assertEqual(device.mem, before)
        self.assertEqual(device.read_raw(0x40, 4), 3)


if __name__ == "__main__":
    unittest.main()
