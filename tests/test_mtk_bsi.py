import importlib.util
from pathlib import Path
import pickle
import sys
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("bsi_test_model",
    Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/hw/bsi.py")
bsi = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bsi
spec.loader.exec_module(bsi)


class BsiTests(unittest.TestCase):
    def test_observe_preserves_ram_behavior(self):
        model = bsi.BsiImmediateControl()
        self.assertEqual(model.read(0x1008, 4), 0)
        for offset, size, value in ((0x1008, 4, 0xabcdef01), (0x1009, 1, 0x12), (0x2002, 2, 0x3456)):
            model.write(offset, size, value)
            self.assertEqual(model.read(offset, size), value)
        model.write(0x1000, 4, 1)
        self.assertEqual(model.facts()["commands"], 1)
        self.assertEqual(model.facts()["pending"], [])
        self.assertEqual(model.read(0x1008, 4), 0xabcd1201)

    def test_pending_never_completes_from_polling(self):
        model = bsi.BsiImmediateControl(mode="pending")
        self.assertEqual(model.read(0x1008, 4), 1)
        model.write(0x1004, 4, 0x1234)
        model.write(0x1014, 4, 31)
        model.write(0x1000, 4, 0x301)
        for _ in range(1000):
            self.assertEqual(model.read(0x1008, 4), 0)
        command = model.facts()["pending"][0]
        self.assertEqual((command["bank"], command["port"], command["data"], command["lengths"]),
                         (0, 3, (0x1234, 0), (31, 0)))
        self.assertEqual(model.facts()["completed_writes"], 0)
        self.assertFalse(model.facts()["backend_connected"])

    def test_banks_and_instances_are_independent(self):
        a, b = [bsi.BsiImmediateControl(mode="pending") for _ in range(2)]
        a.write(0x1000, 4, 1)
        self.assertEqual([a.read(0x1008, 4), a.read(0x1108, 4), b.read(0x1008, 4)], [0, 1, 1])
        a.write(0x1100, 4, 1)
        self.assertEqual(len(a.facts()["pending"]), 2)

    def test_backend_completion_is_explicit_and_sequence_checked(self):
        model = bsi.BsiImmediateControl(mode="pending")
        model.write(0x1000, 4, 1)
        with self.assertRaises(ValueError): model.complete_write(0, 0)
        model.complete_write(0, 1)
        self.assertEqual(model.read(0x1008, 4), 1)
        self.assertEqual(model.read(0x1000, 4), 0)
        with self.assertRaises(ValueError): model.complete_write(0, 1)

    def test_reads_and_extended_requests_are_not_fabricated(self):
        for control in (3, 5):
            model = bsi.BsiImmediateControl(mode="pending")
            model.write(0x1000, 4, control)
            with self.assertRaises(NotImplementedError): model.complete_write(0, 1)
            self.assertEqual(model.read(0x1008, 4), 0)
            self.assertEqual(model.read(0x1204, 4), 0)
            self.assertFalse(model.facts()["rf_emulated"])

    def test_busy_command_cannot_replace_pending_descriptor(self):
        model = bsi.BsiImmediateControl(mode="pending")
        model.write(0x1004, 4, 11)
        model.write(0x1000, 4, 1)
        model.write(0x1004, 4, 22)
        model.write(0x1000, 4, 0x101)
        self.assertEqual(model.facts()["pending"][0]["data"][0], 11)
        self.assertEqual(model.facts()["commands"], 1)
        self.assertEqual(model.facts()["busy_rejections"], 1)

    def test_status_is_readonly_in_pending_mode(self):
        model = bsi.BsiImmediateControl(mode="pending")
        model.write(0x1008, 4, 0)
        self.assertEqual(model.read(0x1008, 4), 1)
        with self.assertRaises(NotImplementedError): model.write(0x1000, 1, 1)

    def test_reset_snapshot_and_bounded_events(self):
        model = bsi.BsiImmediateControl(mode="pending")
        model.write(0x1000, 4, 1)
        for _ in range(200): model.write(0x1000, 4, 1)
        self.assertEqual(len(model.facts()["events"]), 64)
        restored = pickle.loads(pickle.dumps(model))
        self.assertEqual(restored.facts(), model.facts())
        restored.reset()
        self.assertEqual(restored.read(0x1008, 4), 1)
        self.assertEqual(restored.facts()["commands"], 0)

    def test_relocated_three_bank_layout(self):
        model = bsi.BsiImmediateControl(size=0x1000, bank_offsets=(0x100, 0x200, 0x300), mode="pending")
        model.write(0x300, 4, 1)
        self.assertEqual([model.read(x+8, 4) for x in model.banks], [1, 1, 0])

    def test_invalid_access_and_layout_refused(self):
        for banks in ((0x100, 0x100), (0x100, 0x110), (0x101,), (-4,), ()):
            with self.assertRaises(ValueError): bsi.BsiImmediateControl(bank_offsets=banks)
        model = bsi.BsiImmediateControl()
        for offset, size in ((-4, 4), (1, 4), (0x9000, 4), (0, 8)):
            with self.assertRaises(ValueError): model.read(offset, size)
        with self.assertRaises(ValueError): model.write(0, 1, 256)


class BsiAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("avatar2") is None:
            raise unittest.SkipTest("FirmWire runtime required")
        from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
        from firmwire.vendor.mtk.loader import MTKLoader
        from firmwire.vendor.mtk.machine import MT6878Machine
        cls.Device, cls.Loader, cls.Machine = BSIImmediatePeripheral, MTKLoader, MT6878Machine

    def test_adapter_address_independence(self):
        for address in (0x400000, 0x600000):
            device = self.Device("bsi", address, 0x9000, bsi_mode="pending",
                                 firmwire_machine=object.__new__(self.Machine))
            self.assertEqual(device.hw_read(0x1008, 4), 1)
            device.hw_write(0x1000, 4, 1)
            self.assertEqual(device.hw_read(0x1008, 4), 0)

    def test_explicit_loader_selection_and_default(self):
        for mode in ("disabled", "mt6768-observe", "mt6768-pending"):
            loader = object.__new__(self.Loader)
            loader.loader_args, loader.boot_mode = {"bsi": mode}, "native"
            loader.add_memory_range, loader.create_peripheral = Mock(), Mock()
            loader.build_peripheral_maps()
            bank = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa6160000]
            self.assertEqual(len(bank), 1)
            self.assertEqual(bank[0].args[1], 0x9000)
            self.assertEqual("emulate" in bank[0].kwargs, mode != "disabled")

    def test_invalid_selection_refused_before_mapping(self):
        for mode, boot in (("unknown", "native"), ("mt6768-pending", "rehosted")):
            loader = object.__new__(self.Loader)
            loader.loader_args, loader.boot_mode = {"bsi": mode}, boot
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError): loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()
