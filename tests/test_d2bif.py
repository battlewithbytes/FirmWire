import json
import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.d2bif import MTKD2BIFStorageAnalysisPeripheral as Device
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


class D2BIFTests(unittest.TestCase):
    def device(self, base=0x400000, size=0x1000):
        return Device("test", base, size, firmwire_machine=object.__new__(MT6878Machine))

    def test_arbitrary_words_independent_instances_and_unknown_reset(self):
        a, b = self.device(), self.device(0x600000)
        for offset in Device.CONFIG_OFFSETS:
            for device in (a, b):
                with self.assertRaises(NotImplementedError): device.hw_read(offset, 4)
            for value in (0, 1, 47, 56, 0x12345678, 0xffffffff):
                a.hw_write(offset, 4, value)
                b.hw_write(offset, 4, value ^ 0xffffffff)
                self.assertEqual(a.hw_read(offset, 4), value)
                self.assertEqual(b.hw_read(offset, 4), value ^ 0xffffffff)
        a.config.reset()
        with self.assertRaises(NotImplementedError): a.hw_read(0x28, 4)
        self.assertEqual(b.hw_read(0x28, 4), 0)

    def test_strict_other_registers_and_invalid_access(self):
        device = self.device()
        for offset in (0, 4, 0x24, 0x2c, 0x48, 0x50, 0x3e8, 0xfb8):
            with self.assertRaises(NotImplementedError): device.hw_write(offset, 4, 1)
            with self.assertRaises(NotImplementedError): device.hw_read(offset, 4)
        for offset, width, value in ((0x29, 4, 1), (0x28, 1, 1), (0x28, 8, 1),
                                     (0x28, 4, -1), (0x28, 4, 2**32), (0x28, 4, True)):
            with self.assertRaises(ValueError): device.hw_write(offset, width, value)
        self.assertEqual(device.config.writes, 0)
        with self.assertRaises(ValueError): self.device(size=0x4c)

    def test_honest_bounded_detached_observation_and_strict_stop(self):
        device = self.device()
        device.log = Mock()
        for i in range(100): device.hw_write(0x28, 4, i)
        self.assertEqual(device.log.warning.call_count, 32)
        facts = device.control_observation()
        self.assertTrue(facts["analysis_only"])
        for field in ("semantics_verified", "boot_verified", "dma_supported",
                      "completion_fabricated", "guest_irq_routed"):
            self.assertFalse(facts[field])
        self.assertEqual(len(facts["recent_accesses"]), 32)
        facts["recent_accesses"][0]["value"] = -1
        self.assertEqual(device.control_observation()["recent_accesses"][0]["value"], 68)
        device.enable_control_observer()
        with self.assertRaises(NotImplementedError): device.hw_write(0x3e8, 4, 1)
        stop = json.loads(device.log.error.call_args.args[1])
        self.assertEqual(stop["last_unsupported"]["offset"], 0x3e8)
        self.assertEqual(stop["writes"], 100)

    def test_explicit_native_only_loader_selection(self):
        self.assertEqual(MTKLoader.LOADER_ARGS["d2bif"]["default"], "disabled")
        for abi, mode in (("disabled", "native"), ("93xx-storage-analysis", "native"),
                          ("93xx-storage-analysis", "rehosted"), ("other", "native")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"d2bif": abi}, mode
            loader.capability_report = {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            if mode == "rehosted" or abi == "other":
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()
                continue
            loader.build_peripheral_maps()
            mappings = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xab820000]
            self.assertEqual(len(mappings), int(abi != "disabled"))
            if mappings:
                self.assertIs(mappings[0].kwargs["emulate"], Device)
                self.assertTrue(loader.capability_report["d2bif"]["analysis_only"])
