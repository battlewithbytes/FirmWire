import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.lte_timer import (MTKLTETimerGroupCancelAnalysisPeripheral as Device,
                                            MTKLTETimerInitStorageAnalysisPeripheral as Strict)
from firmwire.vendor.mtk.machine import MT6878Machine
from firmwire.vendor.mtk.loader import MTKLoader


class LTECancelTests(unittest.TestCase):
    def device(self, cls=Device, base=0x400000, size=0x2000):
        device = cls("timer", base, size, firmwire_machine=object.__new__(MT6878Machine))
        device.log = Mock()
        return device

    def test_masks_cancel_queued_events_not_configuration_or_irq_status(self):
        a, b = self.device(), self.device(base=0x700000)
        for device in (a, b):
            for bank in range(5): device.group_events.schedule(bank, 0xffffffff, 12)
        for bank, offset in enumerate(Device.GROUP_CANCEL_OFFSETS):
            mask = (0x12345678, 1, 0x80000000, 0, 0xffffffff)[bank]
            self.assertTrue(a.hw_write(offset, 4, mask))
            self.assertTrue(a.hw_write(offset, 4, 0))
            remaining = [e for e in a.group_events.take_due(11)]
            self.assertEqual(remaining, [])
            with self.assertRaises(NotImplementedError): a.hw_read(offset, 4)
        due = a.group_events.take_due(12)
        self.assertEqual(len(due), 160-sum(x.bit_count() for x in (0x12345678, 1, 0x80000000, 0, 0xffffffff)))
        self.assertEqual(len(b.group_events.take_due(12)), 160)
        self.assertEqual(a.config.writes, 0)
        facts = a.control_observation()
        self.assertEqual(facts["group_events"]["cancel_writes"], 10)
        for field in ("configuration_only", "boot_verified", "semantics_verified", "command_semantics_verified",
                      "guest_trigger_supported", "clock_supported", "guest_irq_routed"):
            self.assertFalse(facts[field])

    def test_older_profiles_and_other_commands_remain_strict(self):
        strict, new = self.device(Strict), self.device()
        for offset in Device.GROUP_CANCEL_OFFSETS:
            with self.assertRaises(NotImplementedError): strict.hw_write(offset, 4, 0xffffffff)
        for offset in (0x408, 0x40c, 0x1b98, 0x1b9c, 0x4c4, 0x4e4):
            with self.assertRaises(NotImplementedError): new.hw_write(offset, 4, 1)
        for offset, width, value in ((0x1ba1, 4, 1), (0x1ba0, 1, 1), (0x1ba0, 8, 1),
                                     (0x1ba0, 4, True), (0x1ba0, 4, -1), (0x1ba0, 4, 2**32)):
            with self.assertRaises(ValueError): new.hw_write(offset, width, value)
        with self.assertRaises(ValueError): self.device(size=0x1bd0)

    def test_bounded_observations_and_explicit_native_loader(self):
        device = self.device()
        for i in range(100): device.hw_write(0x1ba0, 4, i)
        self.assertEqual(device.log.warning.call_count, 32)
        facts = device.control_observation()
        self.assertEqual(len(facts["cancel_trace"]), 32)
        facts["cancel_trace"][0]["mask"] = -1
        self.assertEqual(device.control_observation()["cancel_trace"][0]["mask"], 68)
        for mode in ("native", "rehosted"):
            loader = object.__new__(MTKLoader)
            loader.loader_args = {"lte_timer": "93xx-group-cancel-analysis"}
            loader.boot_mode, loader.capability_report = mode, {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            if mode == "rehosted":
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()
            else:
                loader.build_peripheral_maps()
                self.assertTrue(loader.capability_report["lte_timer"]["group_cancel_supported"])
                self.assertTrue(loader.capability_report["lte_timer"]["analysis_only"])
                self.assertFalse(loader.capability_report["lte_timer"]["configuration_only"])
