import json
import pickle
import random
import unittest
from unittest.mock import Mock

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.vendor.mtk.hw.lte_timer import (MTKLTETimerRRPeripheral, MTKLTETimerControlPeripheral,
                                             MTKLTETimerInitStorageAnalysisPeripheral)
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
        for abi in ("disabled", "93xx-rr-config", "93xx-control", "93xx-init-storage-analysis"):
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
                expected = MTKLTETimerControlPeripheral if abi == "93xx-control" else MTKLTETimerRRPeripheral
                if abi == "93xx-init-storage-analysis":
                    expected = MTKLTETimerInitStorageAnalysisPeripheral
                    facts = loader.capability_report["lte_timer"]
                    self.assertTrue(facts["analysis_only"])
                    self.assertFalse(facts["semantics_verified"])
                    self.assertFalse(facts["boot_verified"])
                self.assertIs(mappings[0xa6090000]["emulate"], expected)
        for abi, mode in (("unknown", "native"), ("93xx-rr-config", "rehosted"),
                          ("93xx-control", "rehosted"), ("93xx-init-storage-analysis", "rehosted")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"lte_timer": abi}, mode
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError): loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()

    def test_failure_snapshot_is_opt_in_and_precedes_worker_exception(self):
        device = self.device()
        device.log = Mock()
        with self.assertRaises(NotImplementedError): device.hw_write(0x4a4, 4, 1)
        device.log.error.assert_not_called()
        device.enable_control_observer()
        device.hw_write(0x5c, 4, 0x12345678)
        with self.assertRaises(NotImplementedError): device.hw_write(0x4a4, 4, 1)
        event = json.loads(device.log.error.call_args.args[1])
        self.assertEqual(event["writes"], 1)
        self.assertEqual(event["registers"][0]["value"], 0x12345678)
        self.assertEqual(event["last_unsupported"]["offset"], 0x4a4)
        with self.assertRaises(NotImplementedError): device.hw_read(0x60, 4)
        event = json.loads(device.log.error.call_args.args[1])
        self.assertEqual(event["last_unsupported"]["reason"], "reset value unknown")


class LTETimerControlTests(unittest.TestCase):
    def device(self, base=0x400000):
        return MTKLTETimerControlPeripheral("timer", base, 0x2000,
                                           firmwire_machine=object.__new__(MT6878Machine))

    def test_configuration_readback_masks_and_instance_isolation(self):
        a, b = self.device(), self.device(0x800000)
        rng = random.Random(521)
        for offset in a.CONFIG_OFFSETS:
            with self.assertRaises(NotImplementedError): a.hw_read(offset, 4)
            value = rng.getrandbits(32)
            a.hw_write(offset, 4, value)
            self.assertEqual(a.hw_read(offset, 4), value)
            a.hw_write(offset, 4, 0)
            self.assertEqual(a.hw_read(offset, 4), 0)
            a.hw_write(offset, 4, value)
            self.assertEqual(a.hw_read(offset, 4), value)
        self.assertEqual(len(a.CONFIG_OFFSETS), 33)
        self.assertEqual(a.control_observation()["writes"], 99)
        self.assertTrue(all(r["value"] is None for r in b.control_observation()["registers"]))
        self.assertEqual(pickle.loads(pickle.dumps(a.config)).facts(), a.config.facts())
        a.config.reset()
        self.assertEqual(a.config.facts(), b.config.facts())

    def test_status_clear_clock_and_group_trigger_are_not_storage(self):
        a = self.device()
        for offset in (0, 4, 8, 0x10, 0x58, 0x6c, 0x408, 0x40c, 0x49c,
                       *range(0x4c4, 0x4f4, 4), 0x1b54, 0x1b98, 0x1b9c, 0x1ba0, 0x1ffc):
            for value in (0, 1, 0xffffffff):
                with self.assertRaises(NotImplementedError): a.hw_write(offset, 4, value)
            with self.assertRaises(NotImplementedError): a.hw_read(offset, 4)
        self.assertEqual(a.control_observation()["writes"], 0)
        for key in ("clock_supported", "rr_trigger_supported", "guest_irq_routed",
                    "irq_status_supported", "mode_semantics_verified", "boot_verified"):
            self.assertFalse(a.control_observation()[key])

    def test_snapshot_contains_programmed_masks_without_guessing_unwritten_words(self):
        a = self.device()
        a.log = Mock()
        a.enable_control_observer()
        for i, offset in enumerate(a.SOURCE_MASK_OFFSETS): a.hw_write(offset, 4, 1 << i)
        with self.assertRaises(NotImplementedError): a.hw_write(0x4ec, 4, 0x3f)
        self.assertEqual(a.log.error.call_args.args[0], "LTE control unsupported metadata=%s")
        event = json.loads(a.log.error.call_args.args[1])
        self.assertEqual(event["writes"], 8)
        values = {r["offset"]: r["value"] for r in event["registers"]}
        self.assertEqual([values[o] for o in a.SOURCE_MASK_OFFSETS], [1 << i for i in range(8)])
        self.assertIsNone(values[0x4a0])
        self.assertIsNone(values[0x1b58])
        with self.assertRaises(ValueError):
            MTKLTETimerControlPeripheral("short", 0, 0x1000,
                                         firmwire_machine=object.__new__(MT6878Machine))


class LTEInitHypothesisTests(unittest.TestCase):
    def device(self, base=0x400000):
        device = MTKLTETimerInitStorageAnalysisPeripheral("experiment", base, 0x2000,
                    firmwire_machine=object.__new__(MT6878Machine))
        device.log = Mock()
        return device

    def test_independent_words_arbitrary_values_and_no_fabricated_effects(self):
        a, b = self.device(), self.device(0x800000)
        for offset in a.HYPOTHESIS_OFFSETS:
            with self.assertRaises(NotImplementedError): a.hw_read(offset, 4)
            for value in (0, 1, 0x3f, 0x40, 0x12345678, 0xffffffff):
                a.hw_write(offset, 4, value)
                self.assertEqual(a.hw_read(offset, 4), value)
            with self.assertRaises(NotImplementedError): b.hw_read(offset, 4)
        facts = a.control_observation()
        self.assertEqual(facts["hypothesis_reads"], 12)
        self.assertEqual(facts["hypothesis_writes"], 12)
        self.assertTrue(facts["analysis_only"])
        for key in ("semantics_verified", "boot_verified", "clock_supported",
                    "guest_irq_routed", "expiry_fabricated", "irq_status_supported"):
            self.assertFalse(facts[key])
        a.hw_write(0x4ec, 4, 7)
        self.assertEqual(a.hw_read(0x4f0, 4), 0xffffffff)

    def test_rejections_and_bounded_detached_observations(self):
        a = self.device()
        for offset in (0, 0x4e4, 0x4e8, 0x4f4, 0x1b98):
            with self.assertRaises(NotImplementedError): a.hw_write(offset, 4, 0x3f)
        for size in (1, 2, 8):
            with self.assertRaises(ValueError): a.hw_write(0x4ec, size, 7)
        for value in (-1, 2**32, True):
            with self.assertRaises(ValueError): a.hw_write(0x4ec, 4, value)
        self.assertEqual(a.hypothesis_writes, 0)
        for value in range(100): a.hw_write(0x4ec, 4, value)
        self.assertEqual(len(a.hypothesis_trace), 32)
        self.assertEqual(a.log.warning.call_count, 32)
        facts = a.control_observation()
        facts["hypothesis_trace"][0]["value"] = -1
        self.assertEqual(a.hypothesis_trace[0]["value"], 68)
        self.assertEqual(pickle.loads(pickle.dumps(a.config)).facts(), a.config.facts())
        a.enable_control_observer()
        with self.assertRaises(NotImplementedError): a.hw_write(0, 4, 1)
        event = json.loads(a.log.error.call_args.args[1])
        self.assertTrue(event["analysis_only"])
        self.assertEqual(event["hypothesis_writes"], 100)
