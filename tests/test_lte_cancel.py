import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.lte_timer import (MTKLTETimerGroupCancelAnalysisPeripheral as Device,
                                            MTKLTETimerEventCancelAnalysisPeripheral as EventDevice,
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


class LTEEventCancelTests(unittest.TestCase):
    def device(self, base=0x200000):
        device = EventDevice("events", base, 0x2000, firmwire_machine=object.__new__(MT6878Machine))
        device.log = Mock()
        return device

    def test_selected_event_cancellation_is_distinct_from_groups_and_other_instances(self):
        a, b = self.device(), self.device(0x800000)
        for device in (a, b):
            for bank in range(2): device.individual_events.schedule(bank, 0xffffffff, 50)
            device.group_events.schedule(4, 0xffffffff, 50)
        masks = (0x13579bdf, 0x81234670)
        for bank, offset in enumerate(EventDevice.EVENT_CANCEL_OFFSETS):
            a.hw_write(offset, 4, masks[bank])
            a.hw_write(offset, 4, 0)
        self.assertEqual(a.individual_events.take_due(49), [])
        due = a.individual_events.take_due(50)
        self.assertEqual([(e["bank"], e["bit"]) for e in due],
                         [(bank, bit) for bank in range(2) for bit in range(32) if not masks[bank] & (1 << bit)])
        self.assertEqual(len(b.individual_events.take_due(50)), 64)
        self.assertEqual(len(a.group_events.take_due(50)), 32)
        a.individual_events.schedule(1, 3, 100)
        a.hw_write(0x1bd0, 4, 0xffffffff)
        self.assertEqual(len(a.individual_events.take_due(100)), 2)
        self.assertEqual(a.config.writes, 0)
        facts = a.control_observation()
        self.assertEqual(facts["individual_events"]["cancel_writes"], 4)
        for field in ("boot_verified", "command_semantics_verified", "guest_trigger_supported",
                      "group_to_event_mapping_verified", "timer_readback_supported", "guest_irq_routed"):
            self.assertFalse(facts[field])

    def test_strict_boundaries_bounded_trace_and_loader(self):
        device = self.device()
        strict = Device("strict", 0x200000, 0x2000, firmwire_machine=object.__new__(MT6878Machine))
        for offset in EventDevice.EVENT_CANCEL_OFFSETS:
            with self.assertRaises(NotImplementedError): strict.hw_write(offset, 4, 1)
            with self.assertRaises(NotImplementedError): device.hw_read(offset, 4)
            for width, value in ((1, 1), (8, 1), (4, -1), (4, True), (4, 2**32)):
                with self.assertRaises(ValueError): device.hw_write(offset, width, value)
        for offset in (0, 0x410, 0x44c, 0x450, 0x49c, 0x4c4, 0x1b98, 0x1b9c):
            with self.assertRaises(NotImplementedError): device.hw_write(offset, 4, 1)
            with self.assertRaises(NotImplementedError): device.hw_read(offset, 4)
        for i in range(100): device.hw_write(0x408, 4, i)
        self.assertEqual(device.log.warning.call_count, 32)
        facts = device.control_observation()
        self.assertEqual(len(facts["event_cancel_trace"]), 32)
        facts["event_cancel_trace"][0]["mask"] = -1
        self.assertEqual(device.control_observation()["event_cancel_trace"][0]["mask"], 68)
        self.assertEqual(MTKLoader.LOADER_ARGS["lte_timer"]["default"], "disabled")
        for mode in ("native", "rehosted"):
            loader = object.__new__(MTKLoader)
            loader.loader_args = {"lte_timer": "93xx-event-cancel-analysis"}
            loader.boot_mode, loader.capability_report = mode, {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            if mode == "rehosted":
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()
            else:
                loader.build_peripheral_maps()
                self.assertTrue(loader.capability_report["lte_timer"]["event_cancel_supported"])
                self.assertFalse(loader.capability_report["lte_timer"]["group_to_event_mapping_verified"])
