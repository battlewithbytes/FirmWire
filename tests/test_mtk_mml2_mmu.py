"""Firmware-independent tests: no Lagos PC, hash, table or image required."""
import importlib.util
from pathlib import Path
import pickle
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/hw"
spec = importlib.util.spec_from_file_location("mml2_mmu", ROOT / "mml2_mmu.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Model = module.MML2MMU93Control


class MML2ControlTests(unittest.TestCase):
    def test_command_completes_empty_cache_operation_and_preserves_configuration(self):
        model = Model()
        for offset, value in ((0, 0x12345001), (4, 123), (0x100, 0x55), (0x340, 0x10007)):
            model.write(offset, 4, value)
        for count in range(1, 4):
            model.write(0x40, 4, 0x8000C000)
            self.assertEqual(model.read(0x40, 4), 0xC000)
            self.assertEqual(model.facts()["invalidate_all_completed"], count)
            self.assertTrue(all(model.read(offset, 4) == 0 for offset in Model.CACHE))
        self.assertEqual(model.read(0, 4), 0x12345001)
        self.assertEqual(model.read(4, 4), 123)
        self.assertEqual(model.read(0x100, 4), 0x55)
        self.assertEqual(model.read(0x340, 4), 0x10007)

    def test_polling_does_not_cause_completion_or_change_state(self):
        model = Model()
        before = model.facts()
        for _ in range(300):
            self.assertEqual(model.read(0x40, 4), 0)
        self.assertEqual(model.facts(), before)

    def test_unknown_targeted_and_reserved_commands_never_report_success(self):
        for command in (0, 1, 0xC000, 0x80000000, 0x80000001, 0x80004000, 0x80008000,
                        0x8000C001, 0x8001C000, 0xFFFFFFFF):
            with self.subTest(command=hex(command)):
                model = Model()
                with self.assertRaises(NotImplementedError):
                    model.write(0x40, 4, command)
                self.assertEqual(model.facts()["invalidate_all_completed"], 0)
                self.assertIsNotNone(model.facts()["last_error"])

    def test_partial_command_writes_cannot_bypass_validation(self):
        for offset, size in ((0x40, 1), (0x41, 1), (0x42, 2), (0x43, 1)):
            with self.assertRaises(NotImplementedError):
                Model().write(offset, size, 0)

    def test_access_bounds_alignment_and_value_width(self):
        for offset, size in ((-4, 4), (0x1000, 4), (0, 8), (0, 0), (1, 4), (3, 2), (True, 1)):
            with self.assertRaises(ValueError):
                Model().read(offset, size)
            with self.assertRaises(ValueError):
                Model().write(offset, size, 0)
        for value in (-1, 256, True, 1.5):
            with self.assertRaises(ValueError):
                Model().write(0, 1, value)

    def test_little_endian_configuration_accesses(self):
        model = Model()
        model.write(0, 4, 0x12345678)
        model.write(1, 1, 0xAB)
        model.write(2, 2, 0xCDEF)
        self.assertEqual(model.read(0, 4), 0xCDEFAB78)
        self.assertEqual(model.read(1, 1), 0xAB)
        self.assertEqual(model.read(2, 2), 0xCDEF)

    def test_cache_writes_translation_and_unknown_registers_are_not_fabricated(self):
        model = Model()
        for offset in Model.CACHE | {0x80, 0x300, 0xFFC}:
            with self.assertRaises(NotImplementedError):
                model.write(offset, 4, 1)
        with self.assertRaises(NotImplementedError):
            model.read(0x80, 4)
        with self.assertRaises(NotImplementedError):
            model.translate(0x12345678)
        self.assertFalse(model.facts()["translation_supported"])
        self.assertFalse(model.facts()["dma_supported"])

    def test_reset_and_instance_isolation(self):
        first, second = Model(), Model()
        first.write(0, 4, 123)
        first.write(0x40, 4, 0x8000C000)
        self.assertEqual(second.read(0, 4), 0)
        self.assertEqual(second.facts()["invalidate_all_completed"], 0)
        first.reset()
        self.assertEqual(first.facts(), second.facts())
        self.assertEqual(first.registers, second.registers)


class MML2AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("avatar2") is None:
            raise unittest.SkipTest("adapter tests require the FirmWire runtime")
        from firmwire.vendor.mtk.hw.MML2MMUPeripheral import MML2MMU93Peripheral
        from firmwire.vendor.mtk.machine import MT6878Machine
        from firmwire.vendor.mtk.loader import MTKLoader
        cls.Peripheral, cls.Machine, cls.Loader = MML2MMU93Peripheral, MT6878Machine, MTKLoader

    def test_real_adapter_relocates_and_instances_are_independent(self):
        devices = [self.Peripheral("test", address, 0x1000,
                   firmwire_machine=object.__new__(self.Machine)) for address in (0x200000, 0x400000)]
        devices[0].hw_write(0x40, 4, 0x8000C000)
        self.assertEqual(devices[0].hw_read(0x40, 4), 0xC000)
        self.assertEqual(devices[1].control_facts()["invalidate_all_completed"], 0)
        restored = pickle.loads(pickle.dumps(devices[0].control))
        self.assertEqual(restored.facts(), devices[0].control_facts())
        with self.assertRaises(ValueError):
            self.Peripheral("bad", 0, 0x100, firmwire_machine=object.__new__(self.Machine))

    def test_loader_replaces_exactly_one_bank_only_when_selected(self):
        results = []
        for abi in ("disabled", "93xx-control"):
            loader = object.__new__(self.Loader)
            loader.loader_args = {"mml2_mmu": abi}
            loader.boot_mode = "native"
            loader.add_memory_range, loader.create_peripheral = Mock(), Mock()
            loader.build_peripheral_maps()
            banks = [call for call in loader.add_memory_range.call_args_list if call.args[0] == 0xA0300000]
            self.assertEqual(len(banks), 1)
            results.append(banks[0].kwargs)
        self.assertNotIn("emulate", results[0])
        self.assertIs(results[1]["emulate"], self.Peripheral)

    def test_unknown_abi_and_rehosted_enable_refused_before_mapping(self):
        for abi, mode in (("unknown", "native"), ("93xx-control", "rehosted")):
            loader = object.__new__(self.Loader)
            loader.loader_args = {"mml2_mmu": abi}
            loader.boot_mode = mode
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError):
                loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()


if __name__ == "__main__":
    unittest.main()
