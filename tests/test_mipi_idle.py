"""Undriven serial-line analysis: framing, isolation and explicit assumptions."""
from dataclasses import replace
import unittest

from test_mtk_rf_serial import rf, bsi


def config(level=0):
    return dict(kind="standard-mipi-idle-line-analysis/v1", source="analysis-assumption",
                reason="synthetic undriven-line test, not a target identity", idle_level=level)


def parity(value):
    return value << 1 | (1 ^ (value.bit_count() & 1))


def command(usid=0, address=29, payload=None, port=3):
    read = payload is None
    header = parity(usid << 8 | (3 if read else 2) << 5 | address)
    word = header if read else header << 9 | parity(payload)
    return bsi.SerialCommand(1, 0, port, read, False, (word, 0), (0x8000c if read else 21, 0))


class IdleMipiTests(unittest.TestCase):
    def test_all_standard_addresses_sample_explicit_level_not_register_storage(self):
        for level in (0, 1):
            target = rf.IdleLineMipiTarget(config(level))
            for usid in range(16):
                for address in range(32):
                    write = command(usid, address, (usid * 17 + address) & 255, port=11)
                    self.assertEqual(target.exchange(write).status, "write-complete")
                    read = target.exchange(command(usid, address, port=11))
                    self.assertEqual((read.status, read.value), ("read-complete", level * 0x1ff))
            facts = target.facts()
            self.assertEqual((facts["reads"], facts["writes"]), (512, 512))
            self.assertEqual(len(facts["recent"]), 16)
            for name in ("silicon_verified", "rf_emulated", "target_identity_supplied",
                         "target_registers_modelled", "parity_generated"):
                self.assertFalse(facts[name])

    def test_unknown_frames_do_not_mutate_or_supply_a_reply(self):
        target = rf.IdleLineMipiTarget(config())
        good = command()
        wrong = [replace(good, extended=True), replace(good, read=False),
                 replace(good, data=(good.data[0] ^ 1, 0)),
                 replace(good, data=(1 << 13, 0)), replace(good, data=(True, 0)),
                 replace(good, data=(-1, 0)), replace(good, data=(0,)),
                 replace(good, lengths=(0x8000d, 0)), replace(good, lengths=(0x8000c, 1)),
                 replace(good, lengths=(0x8000c | 1 << 31, 0)),
                 replace(good, lengths=(0x8000c,)), replace(good, lengths=(True, 0)),
                 replace(command(payload=55), data=(command(payload=55).data[0] ^ 1, 0))]
        before = target.facts()
        for trial in wrong:
            reply = target.exchange(trial)
            self.assertEqual(reply.status, "unresolved")
            self.assertIsNone(reply.value)
            self.assertEqual(target.facts(), before)

    def test_controller_completion_and_unconfigured_port_remain_distinct(self):
        endpoint = rf.IdleLineMipiTarget(config())
        model = bsi.BsiImmediateControl(size=0x1000, bank_offsets=(0x100, 0x300), mode="pending",
            read_layout=bsi.ReadCompletionLayout(0x800, 0x804, (1, 7)),
            serial_bus=rf.SerialBus({9: endpoint}))
        for bank, port in ((0, 9), (1, 10)):
            base = model.banks[bank]
            model.write(base+4, 4, command().data[0])
            model.write(base+0x14, 4, 0x8000c)
            model.write(base, 4, port << 8 | 3)
        self.assertEqual(model.read(0x108, 4), 1)
        self.assertEqual(model.read(0x308, 4), 0)
        self.assertEqual(model.read(0x800, 4), 2)
        self.assertEqual(model.completed_reads, 1)
        self.assertEqual(model.facts()["pending_blockers"][0]["reason"], "no-target-on-port")
        for _ in range(100): model.read(0x308, 4)
        self.assertEqual(endpoint.reads, 1)
        model.write(0x804, 4, 2)
        self.assertEqual(model.read(0x800, 4), 0)
        self.assertEqual(len(model.pending), 1)

    def test_configuration_is_opt_in_disjoint_and_detached(self):
        profile = dict(schema="firmwire.software-rf/v1", name="test", analysis_only=True,
                       assumptions="synthetic", rom_sha256="a"*64, ports={"0":dict(chip_id=8,eco=0)})
        self.assertNotIn("idle_mipi_ports", rf.validate_software_rf_profile(profile, "a"*64))
        for bad in (None, [], {"0":config()}, {"03":config()}, {"16":config()}, {3:config()}):
            with self.assertRaises(ValueError):
                rf.validate_software_rf_profile({**profile, "idle_mipi_ports":bad}, "a"*64)
        for key, value in (("idle_level", True), ("idle_level", 2), ("source", "silicon"),
                           ("reason", " "), ("kind", "anything"), ("extra", 0)):
            bad = {**config(), key:value}
            with self.assertRaises(ValueError): rf.IdleLineMipiTarget(bad)
            with self.assertRaises(ValueError):
                rf.validate_software_rf_profile({**profile, "idle_mipi_ports":{"3":bad}}, "a"*64)
        profile["idle_mipi_ports"] = {"3":config()}
        validated = rf.validate_software_rf_profile(profile, "a"*64)
        validated["idle_mipi_ports"]["3"]["idle_level"] = 1
        self.assertEqual(profile["idle_mipi_ports"]["3"]["idle_level"], 0)

    def test_instances_reset_and_snapshot_are_independent(self):
        cfg = config()
        a, b = rf.IdleLineMipiTarget(cfg), rf.IdleLineMipiTarget(config(1))
        cfg["idle_level"] = 1
        self.assertEqual(a.exchange(command()).value, 0)
        self.assertEqual(b.reads, 0)
        snapshot = a.facts()
        snapshot["config"]["idle_level"] = 1
        snapshot["recent"][0]["port"] = 9
        self.assertEqual(a.facts()["recent"][0]["port"], 3)
        self.assertEqual(a.facts()["config"]["idle_level"], 0)
        a.reset()
        self.assertEqual(a.facts()["reads"], 0)
        self.assertEqual(a.facts()["recent"], [])
