import importlib.util
from pathlib import Path
import sys
import unittest


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] /
                                                "firmwire/vendor/mtk/hw" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rf = load("rf_serial_unit", "rf_serial.py")
bsi = load("rf_bsi_unit", "bsi.py")


class RFSerialTests(unittest.TestCase):
    def test_software_identity_is_explicit_and_unwritten_reads_stay_pending(self):
        for chip_id, eco in ((8, 0), (12, 0), (12, 2), (1, 15)):
            target = rf.SoftwareMt6177Target(chip_id, eco)
            m = self.model({5: target})
            self.issue(m, 0x80000)
            self.issue(m, 0x400, read=True)
            self.assertEqual(m.read(0x10c, 4), chip_id | eco << 4)
            m.write(0x404, 4, 2)
            self.issue(m, 0x403, read=True)
            self.assertEqual(m.read(0x108, 4), 0)
            self.assertEqual(target.reads, 1)
            self.assertFalse(target.facts()["silicon_verified"])

    def test_software_readback_requires_guest_write_and_reset_clears_storage(self):
        target = rf.SoftwareMt6177Target(8, 0)
        m = self.model({5: target})
        self.issue(m, (549 << 20) | 0x12345)
        self.issue(m, 0x400 | 549, read=True)
        self.assertEqual(m.read(0x10c, 4), 0x12345)
        m.write(0x404, 4, 2)
        self.issue(m, 0x80000)
        self.assertEqual(target.registers, {})
        self.issue(m, 0x400 | 549, read=True)
        self.assertEqual(m.read(0x108, 4), 0)

    def test_software_invalid_commands_do_not_change_registers(self):
        target = rf.SoftwareMt6177Target(8, 0)
        for word, read, extended in ((1 << 30, False, False), (0, True, False),
                                     (0, False, False), (0x80000, False, True)):
            before = target.facts()
            result = target.exchange(bsi.SerialCommand(1, 0, 0, read, extended, (word, 0), (31, 0)))
            self.assertEqual(result.status, "unresolved")
            self.assertEqual(target.facts(), before)

    def test_software_profile_requires_matching_rom_and_explicit_assumptions(self):
        import copy
        profile = dict(schema="firmwire.software-rf/v1", name="synthetic", analysis_only=True,
                       assumptions="synthetic test", rom_sha256="a"*64,
                       ports={"0": {"chip_id": 8, "eco": 0}})
        validated = rf.validate_software_rf_profile(profile, "a"*64)
        validated["ports"]["0"]["eco"] = 2
        self.assertEqual(profile["ports"]["0"]["eco"], 0)
        for key, value in (("rom_sha256", "b"*64), ("analysis_only", False), ("assumptions", ""),
                           ("ports", {"0": {"chip_id": 8}}),
                           ("ports", {"01": {"chip_id": 8, "eco": 0}}),
                           ("ports", {"0": {"chip_id": True, "eco": 0}}),
                           ("ports", {"0": {"chip_id": 8, "eco": 16}})):
            bad = copy.deepcopy(profile)
            bad[key] = value
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(bad, "a"*64)

    def model(self, targets=None):
        return bsi.BsiImmediateControl(mode="pending", bank_offsets=(0x100, 0x200),
            read_layout=bsi.ReadCompletionLayout(0x400, 0x404, (1, 7)),
            serial_bus=rf.SerialBus(targets if targets is not None else {5: rf.WriteCaptureTarget()}))

    def issue(self, model, word, read=False, port=5, bank=0, extended=False):
        base = model.banks[bank]
        model.write(base+4, 4, word)
        model.write(base, 4, 1 | (2 if read else 0) | (4 if extended else 0) | (port << 8))

    def test_family_write_framing_at_boundaries_not_one_probe_word(self):
        for address in (0, 1, 45, 549, 1023):
            for data in (0, 0x12345, 0xfffff):
                decoded = rf.Mt6177ControlWord.decode((address << 20) | data)
                self.assertEqual((decoded.address, decoded.payload), (address, data))

    def test_read_framing_is_distinct_from_write_framing(self):
        for address in (0, 1, 549, 1023):
            self.assertEqual(rf.Mt6177ControlWord.decode(0x400 | address, read=True).address, address)
        for word in (-1, True, 0, 0x800, 0x1400):
            with self.assertRaises(ValueError): rf.Mt6177ControlWord.decode(word, read=True)
        for word in (-1, True, 1 << 30):
            with self.assertRaises(ValueError): rf.Mt6177ControlWord.decode(word)

    def test_capture_completes_writes_but_never_identity_reads(self):
        model = self.model()
        self.issue(model, 0x23456789)
        self.assertEqual(model.read(0x108, 4), 1)
        self.assertEqual(model.completed, 1)
        self.issue(model, 0x400, read=True)
        for _ in range(100):
            self.assertEqual(model.read(0x108, 4), 0)
            self.assertEqual(model.read(0x400, 4), 0)
        self.assertEqual(model.completed_reads, 0)
        self.assertFalse(model.facts()["rf_emulated"])
        self.assertEqual(model.facts()["serial_targets"]["5"]["writes"], 1)

    def test_unconnected_port_and_extended_write_remain_pending(self):
        model = self.model()
        self.issue(model, 0, port=4)
        self.issue(model, 0, bank=1, extended=True)
        self.assertEqual(len(model.pending), 2)
        self.assertEqual(model.completed, 0)

    def test_reset_clears_target_state_and_pending_commands(self):
        model = self.model()
        self.issue(model, 123)
        self.issue(model, 0x400, read=True)
        model.reset()
        self.assertEqual(model.pending, {})
        self.assertEqual(model.facts()["serial_targets"]["5"]["writes"], 0)

    def test_read_target_replacement_and_ack_preflight(self):
        class SyntheticTarget(rf.WriteCaptureTarget):
            def exchange(self, command):
                self.writes += 1
                return rf.SerialResult("read-complete", 0xa12345678)
        target = SyntheticTarget()
        model = self.model({5: target})
        self.issue(model, 0x400, read=True)
        self.assertEqual(model.read(0x10c, 4), 0x12345678)
        self.assertEqual(model.read(0x110, 4), 0xa)
        self.assertEqual(model.read(0x400, 4), 2)
        self.issue(model, 0x401, read=True)
        self.assertEqual(target.writes, 1)  # no target side effect before guest ack
        model.write(0x404, 4, 2)
        self.assertEqual(model.read(0x400, 4), 0)
        self.assertEqual(len(model.pending), 1)  # ack/polls never re-dispatch

    def test_invalid_backend_reply_is_not_success(self):
        class BadTarget(rf.WriteCaptureTarget):
            def exchange(self, command): return rf.SerialResult("read-complete", 8)
        model = self.model({5: BadTarget()})
        with self.assertRaises(ValueError): self.issue(model, 0)
        self.assertEqual(model.completed, 0)
        self.assertEqual(len(model.pending), 1)

    def test_invalid_bus_configuration_and_observe_backend(self):
        for targets in ({-1: rf.WriteCaptureTarget()}, {16: rf.WriteCaptureTarget()}, {0: object()}):
            with self.assertRaises(ValueError): rf.SerialBus(targets)
        with self.assertRaises(ValueError): bsi.BsiImmediateControl(serial_bus=rf.SerialBus({}))

    def test_instances_do_not_share_capture_state(self):
        a, b = self.model(), self.model()
        self.issue(a, 123)
        self.assertEqual(b.facts()["serial_targets"]["5"]["writes"], 0)
