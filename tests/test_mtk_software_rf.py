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

    def test_shared_clock_refuses_snapshots_before_machine_initialization(self):
        loader = SimpleNamespace(capability_report={"startup_locations_ready":True},
                                 boot_mode="native",loader_args={"bsi":"mt6768-software-rf"})
        for option in ("restore_snapshot","snapshot_at"):
            machine = object.__new__(self.Machine)
            self.assertFalse(machine.initialize(loader,SimpleNamespace(**{option:"test"})))

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

    def test_adapter_propagates_explicit_reset_state_without_seeding_other_registers(self):
        profile = self.profile()
        profile["ports"]["0"]["reset_registers"] = {"341": {
            "value": 0x54321, "source": "analysis-assumption", "reason": "synthetic storage"}}
        d = self.device(profile)
        for word, command in ((0x80000, 1), (0x555, 3)):
            d.hw_write(0x1004, 4, word)
            d.hw_write(0x1000, 4, command)
        self.assertEqual(d.hw_read(0x100c, 4), 0x54321)
        self.assertEqual(d.control_observation()["serial_targets"]["0"]["registers_written"], 0)
        d.hw_write(0x1200, 4, 1)
        d.hw_write(0x1004, 4, 0x556)
        d.hw_write(0x1000, 4, 3)
        self.assertEqual(d.hw_read(0x1008, 4), 0)

    def test_adapter_rcal_profile_and_malformed_calibration_are_checked(self):
        profile = self.profile()
        config = dict(kind="mt6177m-rcal-analysis/v1", source="analysis-assumption", reason="test",
                      cw10=123, cw11=456, trim5=7)
        profile["ports"]["0"]["calibration"] = config
        d = self.device(profile)
        for word in (0x881c00, 0x881c01, 0x900000, 0xc00000):
            d.hw_write(0x1004, 4, word)
            d.hw_write(0x1000, 4, 1)
        d.hw_write(0x1004, 4, 0x40a)
        d.hw_write(0x1000, 4, 3)
        self.assertEqual(d.hw_read(0x100c, 4), 123)
        self.assertEqual(d.control_observation()["serial_targets"]["0"]["calibration"]["config"], config)
        config["trim5"] = 32
        with self.assertRaises(ValueError): self.device(profile)
        self.assertEqual(d.control_observation()["serial_targets"]["0"]["calibration"]["config"]["trim5"], 7)

    def test_characterize_early_read_before_por_write_with_and_without_ldo(self):
        # Characterize the live startup gap, not a desired timing model. Pending
        # commands are dispatched once; a later POR write does not retry them.
        for ldo in (False, True):
            for early in (False, True):
                profile = self.profile()
                if ldo:
                    profile["ports"]["0"]["ldo_calibration"] = dict(
                        kind="mt6177m-ldo-analysis/v1",source="analysis-assumption",reason="test",trims={"8":1})
                d = self.device(profile)
                d.hw_write(0x4000,4,4)
                d.hw_write(0x4020,4,75)
                d.hw_write(0x4024,4,3 << 16 | 3)
                d.hw_write(0x8018,4,173 << 20 | 0x21485)
                d.hw_write(0x801c,4,0)
                d.hw_write(0x4004,4,3)
                if not early: d.advance_guest_blocks(1024)
                d.hw_write(0x1004,4,0x4ad)
                d.hw_write(0x1000,4,3)
                if early: d.advance_guest_blocks(1024)
                facts = d.control_observation()
                self.assertEqual(facts["hwpor"]["completed_writes"],1)
                self.assertEqual(d.control.serial_bus.targets[0].registers[173],0x21485)
                self.assertEqual(d.hw_read(0x1008,4),0 if early else 1)
                self.assertEqual(facts["completed_reads"],0 if early else 1)
                if early:
                    self.assertEqual(facts["pending"][0]["data"][0],0x4ad)
                    self.assertEqual(facts["events"][-1]["reason"],"software-rf-unwritten-register-ad")
                else: self.assertEqual(d.hw_read(0x100c,4),0x21485)

    def test_shared_gcr_clock_flushes_due_por_before_read_without_poll_time(self):
        from firmwire.vendor.mtk.hw.GCRPeripheral import GCRCustom_Periph
        from firmwire.vendor.mtk.hw.guest_clock import bind_rf_clock
        for phase in (0,1,74,75,1023,1024,1025,4095):
            d = self.device(self.profile())
            other = self.device(self.profile())
            gcr = GCRCustom_Periph("gcr",0x1f010000,0x1000,firmwire_machine=d.machine)
            clock = bind_rf_clock({"GCRCustom":gcr,"bsi":d,"other":other})
            gcr.timer = phase
            for off,val in ((0x4000,4),(0x4020,380*75),(0x4024,3<<16|3),
                            (0x8018,0xad21485),(0x801c,0),(0x4004,3)):
                d.hw_write(off,4,val)
            for _ in range(10):
                self.assertEqual(d.hw_read(0x4008,4),0)
                self.assertEqual(gcr.timer,phase)
            for _ in range(379): gcr.hw_read(0x40,4)
            self.assertEqual(d.hwpor.completed_writes,0)
            self.assertEqual(gcr.hw_read(0x40,4),phase+380)
            self.assertEqual(d.hwpor.completed_writes,1)
            d.hw_write(0x1004,4,0x4ad); d.hw_write(0x1000,4,3)
            self.assertEqual(d.hw_read(0x100c,4),0x21485)
            self.assertEqual(other.control.facts()["completed_reads"],0)
            self.assertEqual(other.hwpor.completed_writes,0)
            with self.assertRaises(ValueError): d.advance_guest_blocks(1)
            gcr.timer = (1<<32)+9
            self.assertEqual(gcr.hw_read(0x40,4),10)
            self.assertEqual(gcr.hw_read(0x44,4),1)
            self.assertEqual(gcr.hw_read(0x48,4),10)
            self.assertEqual(gcr.hw_read(0x50,4),11)
            self.assertFalse(clock.facts()["silicon_timing_verified"])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_RF_POR_INVENTORIES"), "needs extracted inventories")
    def test_shared_clock_orders_all_real_por_variants_at_extracted_deadlines(self):
        from firmwire.vendor.mtk.hw.GCRPeripheral import GCRCustom_Periph
        from firmwire.vendor.mtk.hw.guest_clock import bind_rf_clock
        seen = 0
        for path in json.loads(os.environ["FIRMWIRE_RF_POR_INVENTORIES"]):
            doc = json.loads(Path(path).read_text())
            for variant in doc["variants"]:
                seen += 1
                d = self.device(self.profile(doc["rom_sha256"]))
                gcr = GCRCustom_Periph("gcr",0x1f010000,0x1000,firmwire_machine=d.machine)
                bind_rf_clock({"GCRCustom":gcr,"bsi":d})
                expected = {}
                for e,desc,array in zip(variant["event_table"]["records"],
                                        variant["data_table"]["records"],variant["command_arrays"]):
                    i = e["event_index"]
                    d.hw_write(0x4000,4,d.hw_read(0x4000,4)|1<<i)
                    d.hw_write(0x4010+8*i,4,e["time_argument"]*75)
                    d.hw_write(0x4014+8*i,4,e["last_slot"]<<16|e["first_slot"])
                    for index,word in enumerate(array["words"]):
                        slot = e["first_slot"]+index
                        d.hw_write(0x8000+8*slot,4,word["raw"])
                        d.hw_write(0x8004+8*slot,4,desc["port_argument"])
                        expected[word["register_index"]] = word["data"]
                d.hw_write(0x4004,4,3)
                for _ in range(max(e["time_argument"] for e in variant["event_table"]["records"])):
                    gcr.hw_read(0x40,4)
                self.assertEqual(d.hwpor.completed_writes,sum(a["word_count"] for a in variant["command_arrays"]))
                for address,value in expected.items():
                    d.hw_write(0x1004,4,0x400|address); d.hw_write(0x1000,4,3)
                    self.assertEqual(d.hw_read(0x100c,4),value)
                    d.hw_write(0x1200,4,1)
        self.assertEqual(seen,3)

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
