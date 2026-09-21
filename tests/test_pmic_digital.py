"""Relocatable digital PMIC behavior: no image PCs, analog or identity defaults."""
import importlib
from pathlib import Path
import sys
import types
import unittest

package=types.ModuleType("pmic_digital_unit")
package.__path__=[str(Path(__file__).resolve().parents[1]/"firmwire/vendor/mtk/hw")]
sys.modules[package.__name__]=package
p=importlib.import_module(package.__name__+".pmic")
d=importlib.import_module(package.__name__+".pmic_digital")
w=importlib.import_module(package.__name__+".pmic_wacs")
s=importlib.import_module(package.__name__+".pmic_serial")


def target(base=0x100):
    return d.PmicDigitalAnalysis({base:p.PmicRegisterSpec(0xa500,15),
        base+6:p.PmicRegisterSpec(0,0),base+8:p.PmicRegisterSpec(0),
        base+10:p.PmicRegisterSpec(None)},
        aliases={base+2:d.PmicAliasSpec(base,"set"),base+4:d.PmicAliasSpec(base,"clear")},
        key=d.PmicKeySpec(base+6,0x1234,0,frozenset({base+8}),"synthetic protected membership"),
        reject_write_bits={base+8:0x8000},reason="synthetic")


class PmicDigitalTests(unittest.TestCase):
    def test_set_clear_and_masks_at_different_addresses(self):
        for base in (0x100,0x8200):
            t=target(base)
            self.assertEqual(t.write(base+2,0xffff).status,"write-complete")
            self.assertEqual(t.read(base).value,0xa50f)
            t.write(base+4,0xaaaa)
            self.assertEqual(t.read(base).value,0xa505)
            self.assertEqual(t.read(base+2).status,"unresolved")
            self.assertEqual(t.facts()["alias_writes"],2)

    def test_key_gate_blocks_without_mutation_and_relocks(self):
        t=target();before=t.facts()
        self.assertEqual(t.write(0x108,9).reason,"pmic-protected-write-locked")
        self.assertEqual(t.facts(),before)
        t.write(0x106,0x1234);t.write(0x108,9)
        self.assertEqual(t.read(0x108).value,9)
        before=t.facts()
        self.assertEqual(t.write(0x106,5).status,"unresolved")
        self.assertEqual(t.facts(),before)
        t.write(0x106,0)
        self.assertFalse(t.unlocked)
        self.assertEqual(t.write(0x108,1).status,"unresolved")
        self.assertEqual(t.read(0x108).value,9)

    def test_unsupported_side_effect_bit_remains_unresolved(self):
        t=target();t.write(0x106,0x1234);before=t.facts()
        self.assertEqual(t.write(0x108,0x8001).reason,"pmic-write-side-effect-unsupported")
        self.assertEqual(t.facts(),before)

    def test_alias_cannot_bypass_key_gate_or_create_unknown_bits(self):
        for initial in (None,0):
            t=d.PmicDigitalAnalysis({0:p.PmicRegisterSpec(initial),6:p.PmicRegisterSpec(0,0)},
                aliases={2:d.PmicAliasSpec(0,"set")},
                key=d.PmicKeySpec(6,7,0,frozenset({0}),"test"),reason="test")
            self.assertEqual(t.write(2,1).status,"unresolved")
            t.write(6,7)
            self.assertEqual(t.write(2,1).status,"unresolved" if initial is None else "write-complete")

    def test_reset_and_fact_copies_and_bounded_history(self):
        t=target();t.write(0x106,0x1234)
        for i in range(50):t.write(0x108,i)
        facts=t.facts();self.assertEqual(len(facts["recent_writes"]),32)
        facts["recent_writes"][0]["value"]=-1
        self.assertNotEqual(t.facts()["recent_writes"][0]["value"],-1)
        t.reset();self.assertFalse(t.unlocked)
        self.assertEqual(t.read(0x108).value,0)
        self.assertEqual(t.facts()["key_writes"],0)

    def test_wacs_unlock_bsi_write_and_wacs_read_share_gate(self):
        from types import SimpleNamespace
        t=target();a=w.PmicWacsControl(t);b=s.PmicBsiWriteTarget(t)
        a.write(0,0x80000000|(0x106>>1)<<16|0x1234)
        # Same fields consumed by the BSI adapter; transport encoding is separately tested natively.
        command=SimpleNamespace(read=False,extended=False,data=(0x1084567,0))
        self.assertEqual(b.exchange(command).status,"write-complete")
        a.write(0,(0x108>>1)<<16)
        self.assertEqual(a.read(4)&65535,0x4567)
        a.write(8,1);a.write(0,0x80000000|(0x106>>1)<<16)
        before=t.facts()
        self.assertEqual(b.exchange(command).status,"unresolved")
        self.assertEqual(t.facts(),before)

    def test_invalid_alias_key_and_effect_configuration_rejected(self):
        registers={0:p.PmicRegisterSpec(0),6:p.PmicRegisterSpec(0,0)}
        for aliases in ({0:d.PmicAliasSpec(0,"set")},{2:d.PmicAliasSpec(4,"set")},{True:d.PmicAliasSpec(0,"set")}):
            with self.assertRaises(ValueError):d.PmicDigitalAnalysis(registers,aliases=aliases,reason="test")
        for protected in (frozenset(),frozenset({6}),frozenset({True})):
            with self.assertRaises(ValueError):d.PmicKeySpec(6,7,0,protected,"test")
        with self.assertRaises(ValueError):d.PmicAliasSpec(0,"toggle")
        with self.assertRaises(ValueError):d.PmicDigitalAnalysis(registers,aliases={},reject_write_bits={2:1},reason="test")
