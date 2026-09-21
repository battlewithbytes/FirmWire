"""Software contract tests; layout and payloads are deliberately relocatable."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.abbmix import CalibrationLayout, SelectedCalibrationAnalysis
from firmwire.vendor.mtk.hw.AbbMixPeripheral import AbbMixAnalysisPeripheral
from firmwire.vendor.mtk.abbmix_profile import load_abbmix_profile
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


def model(base=0, results=True):
    layout = CalibrationLayout(base+8, base+10, (base+12, base+14), 1, 0x8000, 10, 8, 2)
    return SelectedCalibrationAnalysis(layout, {base:None, base+8:0, base+10:0},
        frozenset({base}), [[11,22,33,44],[100,200,300,400]] if results else None, reason="synthetic")


def profile():
    return dict(schema="firmwire.abbmix-analysis/v1", rom_sha256="a"*64,
        source="analysis-assumption", reason="synthetic", base=0xa619d000, size=4096, modeled_range=[0,16],
        layout=dict(trigger=8, selector=10, status=[12,14], trigger_mask=1,
                    ready_mask=0x8000, result_bits=10, selector_shift=8, selector_bits=2),
        registers={"0":None,"8":0,"10":0}, prerequisites=[0], results=[[11,22,33,44],[100,200,300,400]])


class AbbMixTests(unittest.TestCase):
    def test_only_reviewed_subrange_is_device_rest_preserves_ram(self):
        d=AbbMixAnalysisPeripheral("abb",0x600000,4096,calibration=model(0x800),
            modeled_range=[0x800,0x810],firmwire_machine=object.__new__(MT6878Machine))
        self.assertEqual(d.hw_read(0xa0c,2),0)
        for size in (1,2,4,8):
            d.hw_write(0xa0c,size,(1<<(8*size))-1)
            self.assertEqual(d.hw_read(0xa0c,size),(1<<(8*size))-1)
        self.assertEqual(d.control_observation()["triggers"],0)
        with self.assertRaises(NotImplementedError):d.hw_read(0x802,2)
        with self.assertRaises(ValueError):d.hw_write(0x7ff,2,1)
        with self.assertRaises(ValueError):d.hw_read(4095,2)
        self.assertEqual(d.control_observation()["backing_accesses"],{"read":5,"write":4})

    def test_layout_relocation_and_all_selected_results(self):
        for base in (0,0x800):
            m=model(base)
            self.assertEqual(m.read(base+12,2),0)
            m.write(base,2,0xbeef);m.write(base+8,2,1)
            self.assertTrue(m.ready)
            m.write(base+8,2,0)
            for index in range(4):
                m.write(base+10,2,(index<<8)|0x55)
                self.assertEqual(m.read(base+10,2),(index<<8)|0x55)
                self.assertEqual(m.read(base+12,2)&1023,[11,22,33,44][index])
                self.assertEqual(m.read(base+14,2)&1023,[100,200,300,400][index])
            self.assertEqual(m.facts()["result_reads"],[[1]*4,[1]*4])
            self.assertEqual(m.completions,1)

    def test_missing_backend_or_setup_never_completes_on_poll(self):
        for backend, setup in ((False,True),(True,False)):
            m=model(results=backend)
            if setup:m.write(0,2,9)
            m.write(8,2,1)
            for _ in range(100):self.assertEqual(m.read(12,2),0)
            self.assertTrue(m.pending);self.assertFalse(m.ready)
            self.assertEqual(m.completions,0)
            m.write(8,2,0);self.assertFalse(m.pending)

    def test_repeat_trigger_cancel_reset_and_instance_isolation(self):
        m=model();other=model();m.write(0,2,3);m.write(8,2,1)
        m.write(8,2,1);self.assertEqual(m.completions,1)
        m.write(8,2,0);m.write(8,2,1);self.assertEqual(m.completions,2)
        self.assertFalse(other.ready)
        m.reset();self.assertFalse(m.ready);self.assertEqual(m.completions,0)
        m.write(8,2,1);self.assertTrue(m.pending)

    def test_invalid_accesses_do_not_mutate_register_state(self):
        m=model();before=dict(m.values)
        for off,size,value in ((8,1,1),(9,2,1),(8,4,1),(8,2,None),(8,2,True),(8,2,65536)):
            with self.assertRaises(ValueError):m.write(off,size,value)
        for off in (2,12,14):
            with self.assertRaises(NotImplementedError):m.write(off,2,1)
        with self.assertRaises(NotImplementedError):m.read(0,2)
        self.assertEqual(m.values,before)

    def test_profile_validation_and_detached_bounded_facts(self):
        p=profile()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"profile.json";path.write_text(json.dumps(p))
            _,m,digest=load_abbmix_profile(path,"a"*64,boot_mode="native")
            self.assertEqual(len(digest),64)
            for mode,sha in (("native","b"*64),("rehosted","a"*64)):
                with self.assertRaises(ValueError):load_abbmix_profile(path,sha,boot_mode=mode)
            for field,value in (("results",[[1],[2]]),("prerequisites",[True]),("source","silicon")):
                bad={**p,field:value};path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):load_abbmix_profile(path,"a"*64,boot_mode="native")
        for i in range(50):m.write(0,2,i)
        facts=m.facts();self.assertEqual(len(facts["recent"]),32)
        facts["recent"].clear();self.assertEqual(len(m.facts()["recent"]),32)
        self.assertTrue(facts["analysis_only"]);self.assertFalse(facts["analog_modelled"])

    def test_native_loader_splits_existing_window_without_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"profile.json";p=profile();path.write_text(json.dumps(p))
            loader=object.__new__(MTKLoader);loader.loader_args={"abbmix_analysis_profile":path}
            loader.boot_mode="native";loader.capability_report={"rom_sha256":"a"*64}
            loader.add_memory_range=Mock();loader.create_peripheral=Mock();loader.write_capability_report=Mock()
            loader.build_peripheral_maps()
            calls=[c for c in loader.add_memory_range.call_args_list if 0xa6190000<=c.args[0]<0xa619e000]
            self.assertEqual([(c.args[0],c.args[1]) for c in calls],[(0xa6190000,0xd000),(0xa619d000,0x1000)])
            self.assertIs(calls[1].kwargs["emulate"],AbbMixAnalysisPeripheral)
            p["base"]+=2;path.write_text(json.dumps(p));loader.add_memory_range.reset_mock()
            with self.assertRaises(ValueError):loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()

    def test_peripheral_contract_and_snapshot_refusal(self):
        device=AbbMixAnalysisPeripheral("abb",0x600000,4096,calibration=model(),
                                        firmwire_machine=object.__new__(MT6878Machine))
        device.hw_write(0,2,9);device.hw_write(8,2,1)
        self.assertEqual(device.hw_read(12,2),0x8000|11)
        with self.assertRaises(NotImplementedError):device.__getstate__()
        with self.assertRaises(ValueError):CalibrationLayout(0,2,(4,4),1,8192,12,12,4)
        with self.assertRaises(ValueError):CalibrationLayout(0,2,([],),1,8192,12,12,4)
