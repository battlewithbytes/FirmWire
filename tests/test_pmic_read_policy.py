"""Synthetic read ranges never imply reset facts, registers or write permission."""
import importlib
from pathlib import Path
import sys
import types
import unittest

package = types.ModuleType("pmic_policy_unit")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/hw")]
sys.modules[package.__name__] = package
p = importlib.import_module(package.__name__ + ".pmic")
r = importlib.import_module(package.__name__ + ".pmic_read_policy")
w = importlib.import_module(package.__name__ + ".pmic_wacs")


class PmicReadPolicyTests(unittest.TestCase):
    def target(self, start=0x100, value=0x5a5a):
        registers = {start+2: p.PmicRegisterSpec(None), start+4: p.PmicRegisterSpec(7, 0),
                     start+6: p.PmicRegisterSpec(9, 65535, False), start+8: p.PmicRegisterSpec(0)}
        inner = p.PmicRegisterMapAnalysis(registers, reason="synthetic")
        return r.PmicReadPolicyAnalysis(inner, set(registers), r.PmicReadPolicy(start,start+0x20,2,value,"synthetic"))

    def test_relocated_bounds_stride_and_explicit_unknowns_take_priority(self):
        for start, value in ((0x100,0x5a5a),(0x8000,0x1234)):
            target = self.target(start,value)
            for addr in (start, start+0x20): self.assertEqual(target.read(addr).value,value)
            for addr in (start-2,start+1,start+0x22,start+2,start+6,True,1.0,-1,65536):
                self.assertEqual(target.read(addr).status,"unresolved")
            self.assertEqual(target.read(start+4).value,7)
            self.assertEqual(target.facts()["substituted_reads"],2)

    def test_reads_never_allocate_registers_or_enable_writes(self):
        target=self.target()
        for _ in range(100): target.read(0x100)
        before=target.facts()
        self.assertEqual(len(before["recent_addresses"]),16)
        self.assertEqual(target.write(0x100,123).status,"unresolved")
        self.assertEqual(target.write(0x104,123).status,"unresolved")
        self.assertEqual(target.facts(),before)
        self.assertNotIn("256",before["device"]["registers"])
        self.assertFalse(before["reset_values_verified"])

    def test_channel_reset_preserves_policy_device_reset_clears_counters(self):
        target=self.target()
        channel=w.PmicWacsControl(target)
        channel.write(0,0x80<<16)
        self.assertEqual(channel.read(4)&65535,0x5a5a)
        channel.reset()
        self.assertEqual(target.substituted_reads,1)
        target.write(0x108,99)
        target.reset()
        self.assertEqual(target.substituted_reads,0)
        self.assertEqual(target.read(0x108).value,0)

    def test_explicit_register_writes_and_separate_instances(self):
        a,b=self.target(),self.target()
        self.assertEqual(a.write(0x102,0x4567).status,"write-complete")
        self.assertEqual(a.read(0x102).value,0x4567)
        self.assertEqual(b.read(0x102).status,"unresolved")
        self.assertEqual(a.read(0x100).reason,"explicit-pmic-read-substitution")

    def test_invalid_policies_rejected(self):
        for change in ({"start":True},{"end":65536},{"end":0},{"stride":True},
                       {"stride":3},{"end":0x101},{"value":-1},{"reason":""}):
            args=dict(start=0x100,end=0x120,stride=2,value=0,reason="test");args.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):r.PmicReadPolicy(**args)
