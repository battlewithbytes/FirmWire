"""Profile/adapter contract, with optional extracted Lagos/Coful corpus."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class SoftwareRfAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("avatar2") is None:
            raise unittest.SkipTest("FirmWire runtime required")
        from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
        from firmwire.vendor.mtk.loader import MTKLoader
        from firmwire.vendor.mtk.machine import MT6878Machine
        cls.Device, cls.Loader, cls.Machine = BSIImmediatePeripheral, MTKLoader, MT6878Machine

    def profile(self, sha="a"*64, chip=8, eco=0):
        return dict(schema="firmwire.software-rf/v1", name="synthetic", analysis_only=True,
                    assumptions="test assumptions only", rom_sha256=sha,
                    ports={"0": dict(chip_id=chip, eco=eco)})

    def device(self, profile):
        machine = object.__new__(self.Machine)
        machine.loader = SimpleNamespace(capability_report={"rom_sha256": profile["rom_sha256"]})
        return self.Device("test", 0x600000, 0x9000, bsi_mode="software-rf",
                           rf_profile=profile, firmwire_machine=machine)

    def test_profile_cannot_activate_without_mode_or_matching_rom(self):
        machine = object.__new__(self.Machine)
        machine.loader = SimpleNamespace(capability_report={"rom_sha256": "b"*64})
        for mode, profile in (("pending", self.profile()), ("software-rf", None),
                               ("software-rf", self.profile())):
            with self.assertRaises(ValueError):
                self.Device("test", 0x600000, 0x9000, bsi_mode=mode,
                            rf_profile=profile, firmwire_machine=machine)

    def test_loader_rejects_missing_mismatched_or_implicit_profiles_before_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(self.profile()))
            for mode, profile, sha, boot in (("mt6768-software-rf", None, "a"*64, "native"),
                    ("disabled", path, "a"*64, "native"),
                    ("mt6768-software-rf", path, "b"*64, "native"),
                    ("mt6768-software-rf", path, "a"*64, "rehosted")):
                loader = object.__new__(self.Loader)
                loader.loader_args = dict(bsi=mode, rf_analysis_profile=profile)
                loader.boot_mode = boot
                loader.capability_report = {"rom_sha256": sha}
                loader.add_memory_range = Mock()
                with self.assertRaises(ValueError): loader.build_peripheral_maps()
                loader.add_memory_range.assert_not_called()

    def test_loader_propagates_approved_profile_and_reports_assumptions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(self.profile()))
            loader = object.__new__(self.Loader)
            loader.loader_args = dict(bsi="mt6768-software-rf", rf_analysis_profile=path)
            loader.boot_mode = "native"
            loader.capability_report = {"rom_sha256": "a"*64}
            loader.add_memory_range, loader.create_peripheral, loader.write_capability_report = Mock(), Mock(), Mock()
            loader.build_peripheral_maps()
            bank = next(c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa6160000)
            self.assertEqual(bank.kwargs["rf_profile"], self.profile())
            self.assertEqual(loader.capability_report["software_rf_analysis"], self.profile())

    @unittest.skipUnless(os.environ.get("FIRMWIRE_RF_POR_INVENTORIES"), "needs extracted inventories")
    def test_lagos_coful_both_revisions_actual_words_and_immediate_readback(self):
        paths = json.loads(os.environ["FIRMWIRE_RF_POR_INVENTORIES"])
        self.assertEqual(len(paths), 2)
        seen = 0
        for path in paths:
            doc = json.loads(Path(path).read_text())
            for variant in doc["variants"]:
                seen += 1
                eco = 2 if variant["label"] == "E4" else 0
                chip = 12 if len(doc["variants"]) == 2 else 8  # fixture-only expectations
                d = self.device(self.profile(doc["rom_sha256"], chip, eco))
                expected = {}
                for event, desc, array in zip(variant["event_table"]["records"],
                                               variant["data_table"]["records"], variant["command_arrays"]):
                    index = event["event_index"]
                    d.hw_write(0x4000, 4, d.hw_read(0x4000, 4) | 1 << index)
                    d.hw_write(0x4010 + 8*index, 4, event["time_argument"]*75)
                    d.hw_write(0x4014 + 8*index, 4, event["last_slot"] << 16 | event["first_slot"])
                    for i, word in enumerate(array["words"]):
                        slot = event["first_slot"] + i
                        d.hw_write(0x8000 + 8*slot, 4, word["raw"])
                        d.hw_write(0x8004 + 8*slot, 4, desc["port_argument"])
                        expected[word["register_index"]] = word["data"]
                d.hw_write(0x4004, 4, 3)
                before = d.control_observation()
                for _ in range(10): d.hw_read(0x4008, 4)
                self.assertEqual(d.control_observation(), before)
                d.advance_guest_blocks(0xfffff)
                self.assertEqual(d.hwpor.completed_writes, sum(a["word_count"] for a in variant["command_arrays"]))
                for address, value in {0: chip | eco << 4, **expected}.items():
                    d.hw_write(0x1004, 4, 0x400 | address)
                    d.hw_write(0x1000, 4, 3)
                    self.assertEqual(d.hw_read(0x1204, 4), 1)
                    self.assertEqual(d.hw_read(0x100c, 4), value)
                    d.hw_write(0x1200, 4, 1)
                self.assertFalse(d.control_observation()["firmware_boot_verified"])
        self.assertEqual(seen, 3)
