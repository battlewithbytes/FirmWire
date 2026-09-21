import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.bsi_scheduler import MTKBsiSchedulerCapturePeripheral as Device
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


class BsiSchedulerTests(unittest.TestCase):
    def device(self, base=0x400000, size=0x1000, **kwargs):
        d = Device("test", base, size, firmwire_machine=object.__new__(MT6878Machine), **kwargs)
        d.log = Mock()
        return d

    def test_relocated_layouts_capture_arbitrary_words_without_enable_semantics(self):
        a, b = self.device(), self.device(0x700000, word_offsets=(0x80, 0x20, 0x40))
        for d in (a, b):
            for bank, offset in enumerate(d.word_offsets):
                self.assertIsNone({r["offset"]: r["value"] for r in d.config.facts()["registers"]}[offset])
                for value in (0xffffffff, 0x12345678, 0, 0xa55aa55a):
                    self.assertTrue(d.hw_write(offset, 4, value))
                    captured = {r["offset"]: r["value"] for r in d.config.facts()["registers"]}
                    self.assertEqual(captured[offset], value)
                    self.assertIsNone(d.control_observation()["enable_update_policy"])
        a.hw_write(0, 4, 0)
        self.assertEqual(b.config.facts()["registers"][0]["value"], 0xa55aa55a)

    def test_every_read_and_commands_fail_without_success_or_mutation(self):
        d = self.device()
        for off in d.word_offsets:
            with self.assertRaises(NotImplementedError): d.hw_read(off, 4)
            d.hw_write(off, 4, 0x12345678)
            with self.assertRaises(NotImplementedError): d.hw_read(off, 4)
        before = d.config.facts()
        for off in (0x20, 0x100, 0x1e0, 0x1e8, 0x200, 0x218, 0x8004, 0x1000):
            with self.assertRaises(NotImplementedError): d.hw_write(off, 4, 1)
            with self.assertRaises(NotImplementedError): d.hw_read(off, 4)
        for off, size, value in ((1, 4, 0), (-4, 4, 1), (0, 1, 1), (0, 8, 1),
                                  (0, 4, -1), (0, 4, 2**32), (0, 4, True)):
            with self.assertRaises(ValueError): d.hw_write(off, size, value)
        self.assertEqual(d.config.facts(), before)

    def test_layout_validation_bounded_detached_facts_and_explicit_limits(self):
        for offsets in ((), (0, 0), (-4,), (False,), (1,), (0x1000,), tuple(range(0, 65*4, 4))):
            with self.assertRaises(ValueError): self.device(word_offsets=offsets)
        with self.assertRaises(ValueError): self.device(size=0x1f)
        d = self.device()
        for i in range(200): d.hw_write(0, 4, i)
        self.assertEqual(d.log.warning.call_count, 64)
        facts = d.control_observation()
        self.assertEqual(len(facts["recent_writes"]), 64)
        self.assertTrue(facts["analysis_only"])
        for field in ("semantics_verified", "boot_verified", "reset_known", "readback_supported",
                      "event_identity_mapping_verified", "event_dispatch_supported", "completion_fabricated", "guest_irq_routed"):
            self.assertFalse(facts[field])
        facts["captured_words"]["registers"][0]["value"] = -1
        facts["recent_writes"][0]["value"] = -1
        self.assertEqual(d.control_observation()["captured_words"]["registers"][0]["value"], 199)
        self.assertEqual(d.control_observation()["recent_writes"][0]["value"], 136)

    def test_loader_default_off_native_only_and_separate_from_mipi_capture(self):
        self.assertEqual(MTKLoader.LOADER_ARGS["bsi_scheduler"]["default"], "disabled")
        for abi, mode in (("disabled", "native"), ("mt6768-enable-capture-analysis", "native"),
                          ("mt6768-enable-capture-analysis", "rehosted"), ("mt6768-enable-analysis", "native")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"bsi_scheduler": abi}, mode
            loader.capability_report = {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            if mode != "native" or abi == "mt6768-enable-analysis":
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()
                continue
            loader.build_peripheral_maps()
            mappings = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa6150000]
            self.assertEqual(len(mappings), int(abi != "disabled"))
            self.assertNotIn("mipi", loader.capability_report)
            if mappings:
                self.assertEqual(mappings[0].args[1], 0x1000)
                self.assertIs(mappings[0].kwargs["emulate"], Device)
                self.assertFalse(loader.capability_report["bsi_scheduler"]["event_dispatch_supported"])
