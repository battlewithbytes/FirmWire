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
    def calibration(self, a=0x84210, b=0x739ce, trim=16):
        return dict(kind="mt6177m-rcal-analysis/v1", source="analysis-assumption",
                    reason="synthetic test results; not measured RF", cw10=a, cw11=b, trim5=trim)

    def test_rcal_results_require_ordered_setup_and_do_not_alias_written_control(self):
        for a, b, trim in ((0, 0, 0), (0xfffff, 0xfffff, 31), (0x84210, 0x739ce, 16), (1, 5, 7)):
            config = self.calibration(a, b, trim)
            target = rf.SoftwareMt6177Target(8, 0, calibration=config)
            config["cw10"] ^= 1
            def read(address):
                return target.exchange(bsi.SerialCommand(1, 0, 0, True, False, (0x400 | address, 0), (0, 0)))
            def write(address, payload):
                return target.exchange(bsi.SerialCommand(1, 0, 0, False, False, ((address << 20) | payload, 0), (31, 0)))
            for step, pair in enumerate(rf.Mt6177MRcalAnalysis.SETUP):
                before = target.facts()
                for register in (9, 10, 11):
                    self.assertEqual(read(register).status, "unresolved")
                self.assertEqual(target.facts(), before)  # polling cannot advance calibration
                write(*pair)
                self.assertEqual(target.calibration.phase, step+1)
            self.assertEqual(target.registers[9], 0)
            for _ in range(2):
                for register, expected in ((10, a), (11, b), (9, trim << 10)):
                    reply = read(register)
                    self.assertEqual((reply.status, reply.value), ("read-complete", expected))
                    self.assertEqual(reply.reason, "software-rcal-profile-assumption")
                write(19, a)
                write(20, b)
            self.assertEqual(target.calibration.completions, 1)
            self.assertEqual(read(0x4d).status, "unresolved")  # later LDO stage unsupported
            facts = target.facts()
            facts["calibration"]["config"]["cw10"] ^= 1
            self.assertEqual(target.facts()["calibration"]["config"]["cw10"], a)
            write(0, 0x80000)
            self.assertEqual(read(10).status, "unresolved")
            self.assertEqual(target.calibration.completions, 0)
            self.assertFalse(target.facts()["silicon_verified"])

    def test_rcal_incomplete_reordered_variant_and_invalid_transfers_do_not_complete(self):
        good = rf.Mt6177MRcalAnalysis.SETUP
        for sequence in (good[:3], good[::-1], good[1:], good + ((11, 0),),
                         good + ((8, 0x81c00),), ((8, 0x81c00), (8, 0x81c02), (9, 0), (12, 0))):
            target = rf.SoftwareMt6177Target(8, 0, calibration=self.calibration())
            for address, value in sequence:
                target.exchange(bsi.SerialCommand(1, 0, 0, False, False, ((address << 20) | value, 0), (31, 0)))
            self.assertEqual(target.calibration.read(10).status, "unresolved")
        target = rf.SoftwareMt6177Target(8, 0, calibration=self.calibration())
        for word, extended in ((0x881c00, True), (1 << 30, False)):
            before = target.facts()
            self.assertEqual(target.exchange(bsi.SerialCommand(1, 0, 0, False, extended, (word, 0), (31, 0))).status,
                             "unresolved")
            self.assertEqual(target.facts(), before)

    def test_rcal_validation_is_explicit_and_conflicting_reset_seeds_rejected(self):
        config = self.calibration()
        for key, value in (("kind", "mt6177l-rcal-analysis/v1"), ("source", "silicon"),
                           ("reason", ""), ("cw10", True), ("cw11", -1),
                           ("cw10", 0x100000), ("trim5", 32), ("extra", 1)):
            with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8, 0, calibration={**config, key:value})
        with self.assertRaises(ValueError): rf.validate_rcal_config(None)
        for register in (9, 10, 11):
            seeds = {str(register): self.seed(0)["367"]}
            with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8, 0, seeds, config)
            profile = dict(schema="firmwire.software-rf/v1", name="test", analysis_only=True,
                assumptions="test", rom_sha256="a"*64, ports={"0": dict(chip_id=8, eco=0,
                    reset_registers=seeds, calibration=config)})
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile, "a"*64)

    def test_rcal_target_instances_and_bus_reset_are_independent(self):
        a = rf.SoftwareMt6177Target(8, 0, calibration=self.calibration())
        b = rf.SoftwareMt6177Target(8, 0, calibration=self.calibration(1, 2, 3))
        for address, value in rf.Mt6177MRcalAnalysis.SETUP:
            a.exchange(bsi.SerialCommand(1, 0, 0, False, False, ((address << 20) | value, 0), (31, 0)))
        self.assertEqual(a.calibration.read(10).value, 0x84210)
        self.assertEqual(b.calibration.read(10).status, "unresolved")
        rf.SerialBus({0:a, 1:b}).reset()
        self.assertFalse(a.calibration.facts()["ready"])

    def seed(self, value):
        return {"367": {"value": value, "source": "analysis-assumption",
                        "reason": "synthetic backup/restore test; not a silicon reset value"}}

    def test_explicit_reset_state_backup_restore_and_sor_with_distinct_values(self):
        for value in (0, 0x12345, 0xabcde, 0xfffff):
            seeds = self.seed(value)
            target = rf.SoftwareMt6177Target(8, 0, seeds)
            seeds["367"]["value"] ^= 1  # caller cannot mutate the model
            m = self.model({5: target})
            for reset in (lambda: self.issue(m, 0x80000), m.reset):
                reset()
                self.assertEqual(target.facts()["registers_written"], 0)
                self.issue(m, 0x56f, read=True)
                backup = m.read(0x10c, 4)
                self.assertEqual(backup, value)
                m.write(0x404, 4, 2)
                self.issue(m, (367 << 20) | (value ^ 0xfffff))
                self.issue(m, (367 << 20) | backup)
                self.issue(m, 0x56f, read=True)
                self.assertEqual(m.read(0x10c, 4), value)
                m.write(0x404, 4, 2)
            # Calibration values were NOT granted by configuring storage.
            for register in (9, 10, 11):
                self.issue(m, 0x400 | register, read=True)
                self.assertEqual(m.read(0x108, 4), 0)
                m.reset()
            snapshot = target.facts()
            snapshot["reset_registers"]["367"]["value"] ^= 1
            self.assertEqual(target.facts()["reset_registers"], self.seed(value))
            self.assertFalse(target.facts()["calibration_emulated"])

    def test_reset_seeds_are_validated_at_profile_and_constructor_boundaries(self):
        valid = self.seed(42)["367"]
        bad_maps = [None, [], {"0": valid}, {"01": valid}, {"0x16f": valid},
                    {"1024": valid}, {1: valid}, {str(i): valid for i in range(1, 66)}]
        for key, value in (("value", True), ("value", -1), ("value", 1 << 20),
                           ("source", "silicon"), ("reason", " "), ("extra", 0)):
            bad_maps.append({"367": {**valid, key: value}})
        profile = dict(schema="firmwire.software-rf/v1", name="synthetic", analysis_only=True,
                       assumptions="synthetic test", rom_sha256="a"*64,
                       ports={"0": {"chip_id": 8, "eco": 0}})
        for seeds in bad_maps:
            profile["ports"]["0"]["reset_registers"] = seeds
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile, "a"*64)
            if seeds is not None:  # omitted constructor argument remains backwards compatible
                with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8, 0, seeds)

    def test_reset_state_is_per_target_and_does_not_modify_identity(self):
        a, b = rf.SoftwareMt6177Target(8, 0, self.seed(21)), rf.SoftwareMt6177Target(12, 2, self.seed(99))
        bus = rf.SerialBus({0: a, 2: b})
        a.exchange(bsi.SerialCommand(1, 0, 0, False, False, ((367 << 20) | 7, 0), (31, 0)))
        self.assertEqual(b.registers[367], 99)
        bus.reset()
        self.assertEqual((a.registers[367], b.registers[367]), (21, 99))
        self.assertEqual((a.facts()["cw0"], b.facts()["cw0"]), (8, 0x2c))

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

    def test_pending_reason_survives_retry_history_and_is_detached(self):
        model = self.model()
        self.issue(model, 0xfb, port=3, read=True)
        command = model.pending[0]
        expected = dict(bank=0, sequence=command.sequence, reason="no-target-on-port")
        for _ in range(100):
            self.issue(model, 0xffff, port=4)
            self.assertEqual(model.read(0x108, 4), 0)
        self.assertFalse(any(e["kind"] == "backend_unresolved" for e in model.events))
        self.assertEqual(model.pending[0], command)
        self.assertEqual(model.facts()["pending_blockers"], [expected])
        model.facts()["pending_blockers"][0]["reason"] = "changed"
        self.assertEqual(model.facts()["pending_blockers"], [expected])
        self.assertEqual(model.completed_reads, 0)
        self.assertEqual(model.facts()["serial_targets"]["5"]["writes"], 0)
        model.complete_read(0, command.sequence, 0x123)  # explicit test backend
        self.assertEqual(model.facts()["pending_blockers"], [])
        self.issue(model, 1, port=3)
        model.reset()
        self.assertEqual(model.facts()["pending_blockers"], [])

    def test_pending_reasons_are_bank_local_for_other_layouts_and_ports(self):
        model = bsi.BsiImmediateControl(size=0x2000, bank_offsets=(0x400, 0x800),
                                       mode="pending", serial_bus=rf.SerialBus({}))
        for bank, port in ((0, 9), (1, 12)):
            self.issue(model, 17, port=port, bank=bank)
        self.assertEqual([x["bank"] for x in model.facts()["pending_blockers"]], [0, 1])
        model.complete_write(1, model.pending[1].sequence)
        self.assertEqual([x["bank"] for x in model.facts()["pending_blockers"]], [0])
        self.assertEqual(model.read(0x408, 4), 0)
        self.assertEqual(model.read(0x808, 4), 1)

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
