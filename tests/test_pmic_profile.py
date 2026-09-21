"""Strict PMIC profile validation and real loader composition, no vendor fixture."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


@unittest.skipUnless(importlib.util.find_spec("avatar2"), "requires FirmWire dependencies")
class PmicProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from firmwire.vendor.mtk.pmic_profile import load_pmic_analysis
        from firmwire.vendor.mtk.loader import MTKLoader
        from firmwire.vendor.mtk.machine import MT6878Machine
        from firmwire.vendor.mtk.hw.PMICPeripheral import PMIC_WRAP_Periph
        from firmwire.hw.soc import SOCPeripheral
        cls.load = staticmethod(load_pmic_analysis)
        cls.Loader, cls.Machine, cls.Wrapper, cls.SocPeripheral = MTKLoader, MT6878Machine, PMIC_WRAP_Periph, SOCPeripheral

    def profile(self):
        return dict(schema="firmwire.pmic-analysis/v1", kind="plain-register-map-analysis/v1",
            rom_sha256="a"*64, name="synthetic", source="analysis-assumption", reason="synthetic storage only",
            wrapper_name="TEST_WRAPPER", bsi_port=9, registers={"36":dict(reset_value=0x1357,
                write_mask=65535, readable=True, reason="synthetic initial state")})

    def loader(self, path=None, **overrides):
        loader = object.__new__(self.Loader)
        loader.loader_args = dict(bsi="mt6768-pending", pmic_analysis_profile=path, **overrides)
        loader.boot_mode = "native"
        loader.capability_report = {"rom_sha256":"a"*64}
        loader.modem_soc = SimpleNamespace(peripherals=[self.SocPeripheral(self.Wrapper,
            0x800000, 0x2000, name="TEST_WRAPPER", wacs_init_done_offset=22)])
        loader.add_memory_range, loader.create_peripheral = Mock(), Mock()
        loader.create_soc_peripheral, loader.write_capability_report = Mock(), Mock()
        return loader

    def write(self, directory, config=None):
        path = Path(directory) / "pmic.json"
        path.write_text(json.dumps(self.profile() if config is None else config))
        return path

    def test_profile_hash_and_explicit_policies_are_reported_without_hardware_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory)
            binding = self.load(path,"a"*64,boot_mode="native",bsi_mode="mt6768-pending")
            self.assertEqual(binding.profile_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(binding.target.read(36).value, 0x1357)
            self.assertEqual(binding.target.read(38).status, "unresolved")
            facts = binding.facts()
            self.assertIsNone(facts["hardware_family_selected"])
            self.assertFalse(facts["silicon_verified"])
            self.assertFalse(facts["boot_verified"])
            facts["profile"]["bsi_port"] = 1
            self.assertEqual(binding.bsi_port, 9)

    def test_malformed_wrong_identity_and_noncanonical_profiles_are_rejected(self):
        cases = []
        for key, value in (("schema","bad"), ("kind","mt6358"), ("rom_sha256","b"*64),
                           ("source","hardware"), ("reason"," "), ("name",None), ("extra",True),
                           ("bsi_port",True), ("bsi_port",16), ("wrapper_name","../x"),
                           ("registers",{}), ("registers",[])):
            cases.append({**self.profile(),key:value})
        for address in ("036", "0x24", "65536", "-1"):
            c = self.profile(); c["registers"] = {address:c["registers"]["36"]}; cases.append(c)
        for key, value in (("reset_value",True),("reset_value",65536),("write_mask",-1),
                           ("readable",1),("reason",""),("extra",1)):
            c = self.profile(); c["registers"]["36"][key]=value; cases.append(c)
        with tempfile.TemporaryDirectory() as directory:
            for c in cases:
                with self.subTest(config=c), self.assertRaises(ValueError):
                    self.load(self.write(directory,c),"a"*64,boot_mode="native",bsi_mode="mt6768-pending")
            for raw in ('{"schema":1,"schema":2}', ' '*65537):
                path = Path(directory)/"pmic.json"; path.write_text(raw)
                with self.assertRaises(ValueError):
                    self.load(path,"a"*64,boot_mode="native",bsi_mode="mt6768-pending")

    def test_mode_gates_and_wrapper_selection_fail_before_peripheral_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory)
            for mode in ("disabled","mt6768-observe","mt6768-capture-writes"):
                loader = self.loader(path); loader.loader_args["bsi"] = mode
                with self.assertRaises(ValueError): loader.build_memory_map()
                loader.add_memory_range.assert_not_called()

            for alteration in ("wrong-rom","rehosted","missing","duplicate","wrong-type"):
                loader = self.loader(path)
                if alteration=="wrong-rom": loader.capability_report["rom_sha256"]="b"*64
                if alteration=="rehosted": loader.boot_mode="rehosted"
                if alteration=="missing": loader.modem_soc.peripherals=[]
                if alteration=="duplicate": loader.modem_soc.peripherals *= 2
                if alteration=="wrong-type": loader.modem_soc.peripherals[0]._cls=object
                with self.assertRaises(ValueError): loader.build_memory_map()
                loader.add_memory_range.assert_not_called()

    def test_bounded_read_kind_requires_explicit_policy_and_preserves_strict_writes(self):
        config=self.profile()
        config["kind"]="bounded-read-register-map-analysis/v1"
        config["read_policy"]=dict(start=0,end=100,stride=2,value=0x1234,reason="synthetic range")
        with tempfile.TemporaryDirectory() as directory:
            binding=self.load(self.write(directory,config),"a"*64,boot_mode="native",bsi_mode="mt6768-pending")
            self.assertEqual(binding.target.read(0).value,0x1234)
            self.assertEqual(binding.target.read(36).value,0x1357)
            self.assertEqual(binding.target.write(0,0).status,"unresolved")
            self.assertEqual(binding.facts()["synthetic_read_policy"],config["read_policy"])
            for invalid in (None,{},dict(config["read_policy"],extra=0),dict(config["read_policy"],start=True)):
                bad=copy.deepcopy(config);bad["read_policy"]=invalid
                with self.assertRaises(ValueError):
                    self.load(self.write(directory,bad),"a"*64,boot_mode="native",bsi_mode="mt6768-pending")
            del config["read_policy"]
            with self.assertRaises(ValueError):
                self.load(self.write(directory,config),"a"*64,boot_mode="native",bsi_mode="mt6768-pending")

    def test_loader_passes_one_target_to_actual_wacs_and_bsi_constructors(self):
        with tempfile.TemporaryDirectory() as directory:
            loader = self.loader(self.write(directory))
            original = copy.deepcopy(loader.modem_soc.peripherals[0]._attr)
            self.assertTrue(loader.build_memory_map())
            bank = next(c for c in loader.add_memory_range.call_args_list if c.args[0]==0xa6160000)
            wrapper = next(c for c in loader.create_peripheral.call_args_list if c.kwargs.get("name")=="TEST_WRAPPER")
            target = loader.pmic_analysis_binding.target
            self.assertIs(bank.kwargs["pmic_target"], target)
            self.assertIs(wrapper.kwargs["pmic_target"], target)
            self.assertEqual(loader.modem_soc.peripherals[0]._attr,original)
            machine = object.__new__(self.Machine); machine.loader=loader
            kwargs = {k:v for k,v in bank.kwargs.items() if k not in ("emulate","permissions")}
            bsi = bank.kwargs["emulate"](address=bank.args[0],size=bank.args[1],firmwire_machine=machine,**kwargs)
            wacs = self.Wrapper(address=wrapper.args[1],size=wrapper.args[2],firmwire_machine=machine,**wrapper.kwargs)
            bsi.hw_write(0x1004,4,0x246789); bsi.hw_write(0x1000,4,0x901)
            wacs.hw_write(0xc00,4,0x120000)
            self.assertEqual(wacs.hw_read(0xc04,4),1<<22 | 6<<16 | 0x6789)
            self.assertFalse(loader.capability_report["pmic_analysis"]["boot_verified"])

    def test_absent_profile_keeps_legacy_wrapper_and_no_shared_target(self):
        loader = self.loader()
        loader.build_memory_map()
        loader.create_soc_peripheral.assert_called_once_with(loader.modem_soc.peripherals[0])
        self.assertIsNone(loader.pmic_analysis_binding)
        self.assertNotIn("pmic_analysis",loader.capability_report)
        bank = next(c for c in loader.add_memory_range.call_args_list if c.args[0]==0xa6160000)
        self.assertNotIn("pmic_target",bank.kwargs)

    def test_memory_map_realization_preserves_shared_target_identity(self):
        from firmwire.memory_map import MemoryMap
        from types import MethodType
        with tempfile.TemporaryDirectory() as directory:
            loader = self.loader(self.write(directory))
            loader.memory_map = []
            for name in ("add_memory_range", "create_peripheral", "create_soc_peripheral"):
                setattr(loader, name, MethodType(getattr(MemoryMap, name), loader))
            loader.build_memory_map()
            entries = [entry for entry in loader.memory_map if "pmic_target" in entry.kwargs]
            self.assertEqual(len(entries), 2)
            machine = object.__new__(self.Machine)
            machine.loader, machine.peripheral_map, machine.avatar = loader, {}, Mock()
            machine.avatar.add_memory_range.side_effect = lambda *args, **kw: SimpleNamespace(**kw)
            machine.apply_memory_map(entries)
            bsi = machine.peripheral_map["MODEML1_AO_BSI_MM_2"]
            wacs = machine.peripheral_map["TEST_WRAPPER"]
            bsi.hw_write(0x1004, 4, 0x249abc)
            bsi.hw_write(0x1000, 4, 0x901)
            wacs.hw_write(0xc00, 4, 0x120000)
            self.assertEqual(wacs.hw_read(0xc04, 4), 1 << 22 | 6 << 16 | 0x9abc)
            self.assertEqual(loader.pmic_analysis_binding.target.read(36).value, 0x9abc)

    def test_conflicting_rf_or_mipi_port_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory)
            for conflict in ("rf","mipi"):
                rf = dict(schema="firmwire.software-rf/v1",name="test",analysis_only=True,
                    assumptions="synthetic",rom_sha256="a"*64,ports={"0":dict(chip_id=8,eco=0)})
                if conflict=="rf": rf["ports"]["9"]=dict(chip_id=8,eco=0)
                else: rf["idle_mipi_ports"]={"9":dict(kind="standard-mipi-idle-line-analysis/v1",
                    source="analysis-assumption",reason="synthetic",idle_level=0)}
                rf_path=Path(directory)/"rf.json"; rf_path.write_text(json.dumps(rf))
                loader=self.loader(path,rf_analysis_profile=rf_path)
                loader.loader_args["bsi"]="mt6768-software-rf"
                with self.assertRaisesRegex(ValueError,"conflicts"): loader.build_memory_map()
                loader.add_memory_range.assert_not_called()

    def test_separate_loader_instances_do_not_share_device_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.write(directory)
            a,b=self.loader(path),self.loader(path)
            a.build_memory_map(); b.build_memory_map()
            a.pmic_analysis_binding.target.write(36,0xabcd)
            self.assertEqual(b.pmic_analysis_binding.target.read(36).value,0x1357)

    def test_json_can_relocate_registers_without_changing_device_code(self):
        with tempfile.TemporaryDirectory() as directory:
            for address, value, mask in ((36, 0x1357, 0xffff), (0x724, 0xabc0, 0xf)):
                config = self.profile()
                config["registers"] = {str(address): dict(reset_value=value, write_mask=mask,
                    readable=True, reason="synthetic variant, not a hardware reset claim")}
                binding = self.load(self.write(directory,config), "a"*64,
                                    boot_mode="native", bsi_mode="mt6768-pending")
                self.assertEqual(binding.target.read(address).value, value)
                self.assertEqual(binding.target.write(address, 0x4567).status, "write-complete")
                self.assertEqual(binding.target.read(address).value, (value & ~mask) | (0x4567 & mask))
                other_address = 0x724 if address == 36 else 36
                self.assertEqual(binding.target.read(other_address).status, "unresolved")

    def test_snapshot_requests_are_rejected_before_start_or_restore(self):
        loader=SimpleNamespace(capability_report={"startup_locations_ready":True},boot_mode="native",
                               loader_args={"pmic_analysis_profile":"synthetic"})
        for option in ("snapshot_at","restore_snapshot"):
            machine=object.__new__(self.Machine)
            self.assertFalse(machine.initialize(loader,SimpleNamespace(**{option:"test"})))
        machine=object.__new__(self.Machine); machine.loader=loader
        with self.assertRaisesRegex(RuntimeError,"Shared PMIC"): machine.restore_snapshot("test")
