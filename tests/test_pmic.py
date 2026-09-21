"""Shared PMIC policies and transports; synthetic addresses, no firmware defaults."""
from dataclasses import replace
import importlib
from pathlib import Path
import sys
import types
import unittest

package = types.ModuleType("pmic_component_unit")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/hw")]
sys.modules[package.__name__] = package
p = importlib.import_module(package.__name__ + ".pmic")
s = importlib.import_module(package.__name__ + ".pmic_serial")
bsi = importlib.import_module(package.__name__ + ".bsi")
rf = importlib.import_module(package.__name__ + ".rf_serial")


def target():
    return p.PmicRegisterMapAnalysis({0x24: p.PmicRegisterSpec(0xa5a5),
        0x42: p.PmicRegisterSpec(0xabc0, 0x000f), 0xfffe: p.PmicRegisterSpec(0),
        0x60: p.PmicRegisterSpec(None), 0x62: p.PmicRegisterSpec(None, 0xff),
        0x64: p.PmicRegisterSpec(0x3210, 0), 0x66: p.PmicRegisterSpec(0, readable=False)},
        reason="synthetic policies, not a physical PMIC reset")


def command(address=0x24, value=0x1234):
    return bsi.SerialCommand(1, 0, 9, False, False, (address << 16 | value, 0), (21, 0))


class PmicTests(unittest.TestCase):
    def test_explicit_reset_and_mask_preserve_unwritable_bits(self):
        t = target()
        self.assertEqual(t.read(0x24).value, 0xa5a5)
        self.assertEqual(t.write(0x42, 0xffff).status, "write-complete")
        self.assertEqual(t.read(0x42).value, 0xabcf)
        t.reset()
        self.assertEqual(t.read(0x42).value, 0xabc0)
        self.assertFalse(t.facts()["analog_behavior_modelled"])

    def test_unknowns_and_permissions_do_not_create_state(self):
        t = target()
        before = t.facts()
        for a in (0x60, 0x62, 0x66, 0x22, -1, 65536, True, 36.0):
            self.assertEqual(t.read(a).status, "unresolved")
        for a, v in ((0x62, 8), (0x64, 1), (0x22, 0), (True, 0),
                     (0x24, True), (0x24, -1), (0x24, 65536)):
            self.assertEqual(t.write(a, v).status, "unresolved")
        self.assertEqual(t.facts(), before)
        self.assertEqual(t.write(0x60, 0).status, "write-complete")
        self.assertEqual(t.read(0x60).value, 0)  # known from a full write, not assumed

    def test_config_validation_detachment_and_instance_isolation(self):
        for kwargs in (dict(reset_value=True), dict(reset_value=-1), dict(write_mask=True),
                       dict(write_mask=65536), dict(readable=1)):
            with self.assertRaises(ValueError): p.PmicRegisterSpec(**kwargs)
        for entries in ({}, {True:p.PmicRegisterSpec()}, {65536:p.PmicRegisterSpec()},
                        {0:object()}, {i:p.PmicRegisterSpec() for i in range(257)}):
            with self.assertRaises(ValueError): p.PmicRegisterMapAnalysis(entries, reason="test")
        specs = {2:p.PmicRegisterSpec(7)}
        a = p.PmicRegisterMapAnalysis(specs, reason="test")
        specs[2] = p.PmicRegisterSpec(9)
        a.facts()["registers"]["2"]["value"] = 13
        b = p.PmicRegisterMapAnalysis(specs, reason="test")
        self.assertEqual((a.read(2).value, b.read(2).value), (7, 9))

    def test_bsi_write_wacs_read_and_wacs_write_share_one_device(self):
        t = target()
        serial = s.PmicBsiWriteTarget(t)
        channels = [p.PmicWacsControl(t) for _ in range(2)]
        for address, value in ((0x24, 0x1357), (0xfffe, 0xbeef)):
            self.assertEqual(serial.exchange(command(address, value)).status, "write-complete")
            for w in channels:
                w.write(0, (address >> 1) << 16)
                self.assertEqual(w.read(4), (1 << 21) | (6 << 16) | value)
                w.write(8, 1)
                self.assertEqual(w.read(4), 1 << 21)
            channels[0].write(0, 0x80000000 | (address >> 1) << 16 | (value ^ 0xffff))
            self.assertEqual(channels[0].state, 0)
            channels[1].write(0, (address >> 1) << 16)
            self.assertEqual(channels[1].data, value ^ 0xffff)
            channels[1].write(8, 1)
        self.assertEqual(serial.facts()["writes"], 2)

    def test_unknown_read_stays_pending_until_explicit_backend_retry(self):
        t = target()
        w = p.PmicWacsControl(t)
        w.write(0, (0x60 >> 1) << 16)
        before = w.facts(), t.facts()
        for _ in range(200): self.assertEqual(w.read(4), 1 << 21 | 2 << 16)
        self.assertEqual((w.facts(), t.facts()), before)
        for offset, value in ((0, 0), (8, 1)):
            with self.assertRaises(ValueError): w.write(offset, value)
        s.PmicBsiWriteTarget(t).exchange(command(0x60, 0x2468))
        self.assertEqual(w.state, 2)  # no implicit poll-triggered completion
        self.assertTrue(w.retry_pending())
        self.assertEqual((w.state, w.data), (6, 0x2468))
        w.write(8, 1)
        with self.assertRaises(ValueError): w.retry_pending()

    def test_transport_reset_does_not_reset_shared_target_or_other_channel(self):
        t = target()
        a, b = p.PmicWacsControl(t), p.PmicWacsControl(t)
        serial = s.PmicBsiWriteTarget(t)
        serial.exchange(command(value=0x6789))
        a.write(0, 0x12 << 16)
        b.write(0, 0x12 << 16)
        a.reset()
        serial.reset()
        self.assertEqual((b.state, b.data), (6, 0x6789))
        self.assertEqual(t.read(0x24).value, 0x6789)
        t.reset()  # only the device owner performs a whole-device reset
        self.assertEqual(t.read(0x24).value, 0xa5a5)

    def test_bsi_rejects_unknown_framing_and_does_not_supply_read_values(self):
        t = target()
        serial = s.PmicBsiWriteTarget(t)
        before = t.facts(), serial.facts()
        for c in (replace(command(), read=True), replace(command(), extended=True),
                  replace(command(), data=(0x241234, 1)), replace(command(), data=(True, 0)),
                  replace(command(), data=(-1, 0)), replace(command(), data=(1 << 32, 0)),
                  replace(command(), data=(0,)), command(0x9999, 0)):
            response = serial.exchange(c)
            self.assertEqual(response.status, "unresolved")
            self.assertIsNone(response.value)
            self.assertEqual((t.facts(), serial.facts()), before)
        for lengths in ((0, 0), (21, 0), (0x8000c, 0), (31, 0)):
            self.assertEqual(serial.exchange(replace(command(), lengths=lengths)).status, "write-complete")

    def test_relocated_bsi_controller_completes_only_supported_writes(self):
        t = target()
        serial = s.PmicBsiWriteTarget(t)
        m = bsi.BsiImmediateControl(size=0x1000, bank_offsets=(0x100, 0x300), mode="pending",
            read_layout=bsi.ReadCompletionLayout(0x800, 0x804, (1, 7)), serial_bus=rf.SerialBus({9:serial}))
        m.write(0x104, 4, 0x241234)
        m.write(0x100, 4, 0x901)
        self.assertEqual(m.read(0x108, 4), 1)
        self.assertEqual(t.read(0x24).value, 0x1234)
        m.write(0x304, 4, 0x99990000)
        m.write(0x300, 4, 0x901)
        self.assertEqual(m.read(0x308, 4), 0)
        self.assertEqual(m.read(0x800, 4), 0)
        self.assertEqual(m.facts()["pending_blockers"][0]["reason"], "pmic-register-write-unsupported")
        m.reset()
        self.assertEqual(t.read(0x24).value, 0x1234)

    def test_wacs_access_checks_and_valid_clear_keep_latched_data(self):
        w = p.PmicWacsControl(target(), init_done_offset=22)
        for offset in (0, 8, 12, True, 4.0):
            with self.assertRaises(ValueError): w.read(offset)
        for offset, value in ((12, 0), (True, 0), (0, True), (0, -1), (0, 1 << 32), (8, 1)):
            with self.assertRaises(ValueError): w.write(offset, value)
        w.write(0, 0x12 << 16)
        before = w.facts()
        for offset, value in ((0, 0), (8, 0), (8, 2)):
            with self.assertRaises(ValueError): w.write(offset, value)
        self.assertEqual(w.facts(), before)
        self.assertEqual(w.read(4), 1 << 22 | 6 << 16 | 0xa5a5)
        for bit in (16, 17, 18, 19, 20, 32, True):
            with self.assertRaises(ValueError): p.PmicWacsControl(target(), init_done_offset=bit)

    def test_target_failure_never_turns_into_transport_completion(self):
        class Broken(p.PmicTarget):
            def read(self, address): return p.PmicResult("read-complete", True)
            def write(self, address, value): return p.PmicResult("write-complete", 0)
            def reset(self): pass
            def facts(self): return {}
        w = p.PmicWacsControl(Broken())
        with self.assertRaises(ValueError): w.write(0, 0)
        self.assertEqual(w.state, 2)
        self.assertEqual(w.completed_reads, 0)
        serial = s.PmicBsiWriteTarget(Broken())
        with self.assertRaises(ValueError): serial.exchange(command())
        self.assertEqual(serial.writes, 0)


if __name__ == "__main__":
    unittest.main()
