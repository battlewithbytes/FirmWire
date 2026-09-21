import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.mipi import MTKMipiInitCapturePeripheral as Device
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


class MipiCaptureTests(unittest.TestCase):
    def device(self, base=0x400000, size=0x5000, **kwargs):
        device = Device("test", base, size, firmwire_machine=object.__new__(MT6878Machine), **kwargs)
        device.log = Mock()
        return device

    def test_arbitrary_values_layouts_and_independent_instances(self):
        a = self.device()
        b = self.device(0x600000, 0x1000, bank_offsets=(0x100, 0x300, 0x600))
        for device in (a, b):
            for bank in device.bank_offsets:
                for word in device.WORD_OFFSETS:
                    for value in (0, 1, 0x13579bdf, 0xffffffff):
                        self.assertTrue(device.hw_write(bank+word, 4, value))
        a.hw_write(4, 4, 123)
        self.assertEqual(b.control_observation()["registers"][0]["value"], 0xffffffff)
        self.assertEqual(len(a.control_observation()["registers"]), 40)
        self.assertEqual(len(b.control_observation()["registers"]), 24)

    def test_all_reads_unknown_offsets_and_bad_access_stop_without_writes(self):
        d = self.device()
        for bank in d.bank_offsets:
            for offset in d.WORD_OFFSETS:
                with self.assertRaises(NotImplementedError): d.hw_read(bank+offset, 4)
            for offset in (0, 8, 0x14, 0x40, 0x54, 0x94, 0xffc):
                with self.assertRaises(NotImplementedError): d.hw_write(bank+offset, 4, 0)
        for offset, size, value in ((5, 4, 1), (4, 1, 1), (4, 8, 1),
                                    (4, 4, -1), (4, 4, 2**32), (4, 4, True)):
            with self.assertRaises(ValueError): d.hw_write(offset, size, value)
        self.assertEqual(d.config.writes, 0)
        d.hw_write(4, 4, 0xabcdef01)
        with self.assertRaises(NotImplementedError): d.hw_read(4, 4)
        self.assertEqual(d.config.reads, 0)

    def test_layout_validation(self):
        for banks in ((), (0, 0), (-4,), (1,), (False,), (0, 4), (0x5000,), tuple(i*0x100 for i in range(17))):
            with self.subTest(banks=banks), self.assertRaises(ValueError): self.device(bank_offsets=banks)
        with self.assertRaises(ValueError): self.device(size=0x4090)

    def test_bounded_detached_facts_do_not_claim_hardware_effects(self):
        d = self.device()
        for i in range(150): d.hw_write(4, 4, i)
        facts = d.control_observation()
        self.assertEqual(d.log.warning.call_count, 64)
        self.assertEqual(len(facts["recent_accesses"]), 64)
        self.assertTrue(facts["analysis_only"])
        for key in ("semantics_verified", "boot_verified", "readback_supported",
                    "serial_transactions_supported", "completion_fabricated", "guest_irq_routed"):
            self.assertFalse(facts[key])
        facts["registers"][0]["value"] = -1
        facts["recent_accesses"][0]["value"] = -1
        facts["bank_offsets"].clear()
        self.assertEqual(d.control_observation()["registers"][0]["value"], 149)
        self.assertEqual(d.control_observation()["recent_accesses"][0]["value"], 86)
        with self.assertRaises(NotImplementedError): d.hw_read(4, 4)
        self.assertEqual(d.control_observation()["capture_stop"]["direction"], "read")

    def test_native_only_default_off_separate_mapping(self):
        self.assertEqual(MTKLoader.LOADER_ARGS["mipi"]["default"], "disabled")
        for abi, mode in (("disabled", "native"), ("mt6768-init-capture-analysis", "native"),
                          ("mt6768-init-capture-analysis", "rehosted"), ("other", "native")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"mipi": abi}, mode
            loader.capability_report = {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            if mode != "native" or abi == "other":
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()
                continue
            loader.build_peripheral_maps()
            mappings = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa6173000]
            self.assertEqual(len(mappings), int(abi != "disabled"))
            lower = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa6170000]
            self.assertEqual(lower[0].args[1], 0x3000)
            self.assertNotIn("emulate", lower[0].kwargs)
            if mappings:
                self.assertEqual(mappings[0].args[1], 0x5000)
                self.assertIs(mappings[0].kwargs["emulate"], Device)
                self.assertTrue(loader.capability_report["mipi"]["analysis_only"])
