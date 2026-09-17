"""Firmware-free protocol tests: no PCs or firmware table writes in the model."""
import unittest
from test_mtk_rf_serial import rf, bsi


def config(value=7):
    return dict(kind="mt6177m-ldo-analysis/v1", source="analysis-assumption",
                reason="synthetic test trims", trims={s: min(value, (1 << spec[0])-1)
                    for s, spec in rf.Mt6177MLdoAnalysis.SELECTORS.items()})


class LdoTests(unittest.TestCase):
    def target(self, cfg=None):
        return rf.SoftwareMt6177Target(8, 0, ldo_calibration=config() if cfg is None else cfg)

    def write(self, t, address, value):
        return t.exchange(bsi.SerialCommand(1, 0, 0, False, False, ((address << 20) | value, 0), (31, 0)))

    def read(self, t, address=77):
        return t.exchange(bsi.SerialCommand(1, 0, 0, True, False, (0x400 | address, 0), (0, 0)))

    def setup(self, t, selector, prepare=None, trigger=None):
        _, p, arm, start = rf.Mt6177MLdoAnalysis.SELECTORS[str(selector)]
        for a, v in ((15, p if prepare is None else prepare), (75, 0x80000),
                     (15, arm), (76, selector), (75, start if trigger is None else trigger)):
            self.write(t, a, v)

    def test_every_reviewed_selector_zero_max_and_distinct_values(self):
        for value in (0, 7, 31):
            cfg = config(value)
            t = self.target(cfg)
            cfg["trims"]["8"] ^= 1
            for selector, (width, prepare, arm, start) in rf.Mt6177MLdoAnalysis.SELECTORS.items():
                self.write(t, 15, prepare)
                before = t.facts()
                self.assertEqual(self.read(t).status, "unresolved")
                self.assertEqual(t.facts(), before)
                self.write(t, 75, 0x80000)
                self.assertEqual(self.read(t).value, 0)
                self.write(t, 15, arm)
                self.assertEqual(self.read(t).status, "unresolved")
                self.write(t, 76, int(selector))
                self.write(t, 75, start)
                self.write(t, 76, 0)  # RX clears selection before result read
                self.write(t, 367, 0x12345)  # unrelated analog setup is not state advancement
                for _ in range(2):
                    self.assertEqual(self.read(t).value, min(value, (1 << width)-1) << 15)
                self.write(t, 15, 0x800)
                self.assertEqual(self.read(t).status, "unresolved")
            facts = t.facts()["ldo_calibration"]
            self.assertEqual((facts["completions"], facts["clear_reads"], facts["result_reads"]), (15, 15, 30))
            self.assertEqual(facts["completed_selectors"], dict.fromkeys(config()["trims"], 1))
            facts["config"]["trims"]["8"] = 100
            self.assertNotEqual(t.facts()["ldo_calibration"]["config"], facts["config"])

    def test_wrong_sequence_trigger_selector_mode_and_result_write_invalidate(self):
        for pairs in (((75, 0x40000),), ((15, 0x5800), (76, 8), (75, 0x60000)),
                      ((15, 0x5800), (75, 0x80000), (15, 0x1800), (76, 3), (75, 0x40000))):
            t = self.target()
            for a, v in pairs: self.write(t, a, v)
            self.assertEqual(self.read(t).status, "unresolved")
        for selector, kwargs in ((8, {"trigger":0x40000}), (0x40, {"prepare":0x5800})):
            t = self.target()
            self.setup(t, selector, **kwargs)
            self.assertEqual(self.read(t).status, "unresolved")
        for pair in ((77, 0x12345), (76, 4), (75, 0x40000), (15, 0x800)):
            t = self.target()
            self.setup(t, 8)
            self.write(t, *pair)
            self.assertEqual(self.read(t).status, "unresolved")
        cfg = config(); del cfg["trims"]["8"]
        t = self.target(cfg); self.setup(t, 8)
        self.assertEqual(self.read(t).status, "unresolved")

    def test_reset_isolation_disabled_model_and_unknown_next_stage(self):
        a, b = self.target(), self.target()
        self.setup(a, 8)
        self.assertEqual(self.read(b).status, "unresolved")
        self.assertEqual(self.read(a, 0x1bf).status, "unresolved")
        self.write(a, 0, 0x80000)
        self.assertEqual(self.read(a).status, "unresolved")
        self.assertEqual(a.ldo_calibration.completions, 0)
        self.setup(a, 8); rf.SerialBus({0:a, 1:b}).reset()
        self.assertEqual(self.read(a).status, "unresolved")
        disabled = rf.SoftwareMt6177Target(8, 0)
        self.setup(disabled, 8)
        self.assertEqual(self.read(disabled).status, "unresolved")

    def test_configuration_and_profile_validation(self):
        cfg = config()
        bad = [None, {}, {**cfg,"kind":"mt6177l-ldo-analysis/v1"}, {**cfg,"source":"silicon"},
               {**cfg,"reason":" "}, {**cfg,"extra":0}, {**cfg,"trims":{}},
               *({**cfg,"trims":x} for x in ({"8":16}, {"128":32}, {"08":1}, {8:1},
                                             {"3":1}, {"8":True}, {"8":-1}))]
        profile = dict(schema="firmwire.software-rf/v1", name="test", analysis_only=True,
                       assumptions="test", rom_sha256="a"*64, ports={"0":dict(chip_id=8,eco=0)})
        for item in bad:
            with self.assertRaises(ValueError): rf.validate_ldo_config(item)
            profile["ports"]["0"]["ldo_calibration"] = item
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile, "a"*64)
        profile["ports"]["0"]["ldo_calibration"] = cfg
        self.assertEqual(rf.validate_software_rf_profile(profile,"a"*64),profile)
        seed = {"77":dict(value=0,source="analysis-assumption",reason="test")}
        with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8,0,seed,ldo_calibration=cfg)
        profile["ports"]["0"]["reset_registers"] = seed
        with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile,"a"*64)


if __name__ == "__main__": unittest.main()
