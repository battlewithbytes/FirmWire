import pickle
import random
import unittest
from unittest.mock import Mock

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.vendor.mtk.hw.lte_timer import MTKLTETimerRRPeripheral
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


class ConfigurationTests(unittest.TestCase):
    def test_sparse_layout_and_unknown_reset(self):
        for offsets in ((0, 4, 20), (0x100, 0x200), (0xfffffffc,)):
            model = ConfigurationRegisters(offsets)
            for offset in offsets:
                with self.assertRaises(NotImplementedError): model.read(offset, 4)
            self.assertTrue(all(r["value"] is None for r in model.facts()["registers"]))
            for offset in offsets:
                model.write(offset, 4, offset)
                self.assertEqual(model.read(offset, 4), offset)

    def test_validation_and_atomic_rejection(self):
        for offsets in ((), (0, 0), (-4,), (2,), (True,), (2**32,), range(0, 4100, 4)):
            with self.assertRaises(ValueError): ConfigurationRegisters(offsets)
        model = ConfigurationRegisters((0x5c,))
        model.write(0x5c, 4, 7)
        for offset, size in ((0x5c, 1), (0x5c, 2), (0x5c, 8), (0x5d, 4),
                             (-4, 4), (True, 4), (2**32, 4)):
            with self.assertRaises(ValueError): model.write(offset, size, 3)
            with self.assertRaises(ValueError): model.read(offset, size)
        for value in (True, -1, 2**32, 1.0):
            with self.assertRaises(ValueError): model.write(0x5c, 4, value)
        self.assertEqual(model.read(0x5c, 4), 7)
        self.assertEqual(model.writes, 1)

    def test_snapshot_reset_and_bounded_counters(self):
        model = ConfigurationRegisters((4, 12))
        model.write(4, 4, 0xffffffff)
        with self.assertRaises(NotImplementedError): model.write(8, 4, 3)
        facts = model.facts()
        self.assertEqual(pickle.loads(pickle.dumps(model)).facts(), facts)
        facts["registers"][0]["value"] = 0
        facts["last_unsupported"]["offset"] = 4
        self.assertEqual(model.read(4, 4), 0xffffffff)
        self.assertEqual(model.facts()["last_unsupported"]["offset"], 8)
        model.reads = model.writes = 2**64 - 1
        model.read(4, 4)
        model.write(4, 4, 0)
        self.assertEqual(model.reads, model.writes)
        self.assertEqual(model.writes, 2**64 - 1)
        model.reset()
        self.assertEqual(model.facts(), ConfigurationRegisters((4, 12)).facts())


class LTETimerTests(unittest.TestCase):
    def device(self, base=0xa6090000):
        return MTKLTETimerRRPeripheral("timer", base, 0x2000,
                                       firmwire_machine=object.__new__(MT6878Machine))

    def test_banks_preserve_arbitrary_words_without_triggering(self):
        rng = random.Random(0x12345)
        a, b = self.device(), self.device(0x400000)
        expected_offsets = (0x5c, 0x60, 0x64, 0x68, 0x70, 0x74, 0x78, 0x7c)
        self.assertEqual(a.RR_OFFSETS, expected_offsets)
        for _ in range(100):
            values = [rng.getrandbits(32) for _ in expected_offsets]
            for offset, value in zip(expected_offsets, values): a.hw_write(offset, 4, value)
            self.assertEqual([a.hw_read(x, 4) for x in expected_offsets], values)
        self.assertTrue(all(r["value"] is None for r in b.control_observation()["registers"]))
        facts = a.control_observation()
        for key in ("clock_supported", "rr_trigger_supported", "guest_irq_routed",
                    "expiry_fabricated", "boot_verified"):
            self.assertFalse(facts[key])

    def test_commands_status_and_non_rr_configuration_remain_strict(self):
        device = self.device()
        for offset in (0, 4, 8, 0x58, 0x6c, 0x80, 0x408, 0x40c, 0x4a0,
                       0x4a4, 0x4ec, 0x1b58, 0x1b98, 0x1ba0, 0x1bd0, 0x1ffc):
            for value in (0, 1, 2, 0xffffffff):
                with self.assertRaises(NotImplementedError): device.hw_write(offset, 4, value)
            with self.assertRaises(NotImplementedError): device.hw_read(offset, 4)
        self.assertEqual(device.control_observation()["writes"], 0)

    def test_loader_selection_is_explicit_and_native_only(self):
        self.assertEqual(MTKLoader.LOADER_ARGS["lte_timer"]["default"], "disabled")
        for abi in ("disabled", "93xx-rr-config"):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"lte_timer": abi}, "native"
            loader.capability_report = {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            loader.build_peripheral_maps()
            mappings = {c.args[0]: c.kwargs for c in loader.add_memory_range.call_args_list}
            self.assertEqual(0xa6090000 in mappings, abi != "disabled")
            if abi != "disabled":
                self.assertIs(mappings[0xa6090000]["emulate"], MTKLTETimerRRPeripheral)
        for abi, mode in (("unknown", "native"), ("93xx-rr-config", "rehosted")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"lte_timer": abi}, mode
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError): loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()
