"""Family protocol tests independent of firmware PCs and runtime tables."""
import unittest
from test_mtk_rf_serial import rf, bsi


def config(trim=23):
    return dict(kind="mt6177m-rx-rc-analysis/v1", source="analysis-assumption",
                reason="synthetic test trim, not an RF measurement", trim6=trim)


class RxRcTests(unittest.TestCase):
    def target(self, trim=23):
        return rf.SoftwareMt6177Target(8, 0, rx_rc_calibration=config(trim))

    def write(self, target, address, value):
        return target.exchange(bsi.SerialCommand(1, 0, 0, False, False,
                              ((address << 20) | value, 0), (31, 0)))

    def read(self, target, address=447):
        return target.exchange(bsi.SerialCommand(1, 0, 0, True, False,
                              (0x400 | address, 0), (0, 0)))

    def setup(self, target):
        # Independent sequence from reviewed consumer, not the model constant.
        for pair in ((1,0x112a0),(320,0),(321,0),(467,0x2c01),(1,0x212a8)):
            self.write(target, *pair)

    def test_all_six_bit_results_and_repeated_sequences(self):
        for trim in range(64):
            target = self.target(trim)
            for iteration in range(2):
                before = target.facts()
                self.assertEqual(self.read(target).status, "unresolved")
                self.assertEqual(target.facts(), before)
                self.setup(target)
                self.write(target, 367, 0x12345)  # unrelated write must not change state
                for _ in range(2):
                    self.assertEqual(self.read(target).value, trim << 14)
                self.write(target, 447, (trim << 14) | (trim << 8))
                self.assertEqual(self.read(target).status, "unresolved")
                self.write(target, 467, 1)
                self.write(target, 1, 0x112a0)
                facts = target.facts()["rx_rc_calibration"]
                self.assertEqual((facts["completions"], facts["reads"]), (iteration+1,2*(iteration+1)))
                self.assertFalse(facts["analog_calibration_verified"])

    def test_partial_wrong_reordered_and_invalidated_sequences(self):
        pairs = [(1,0x112a0),(320,0),(321,0),(467,0x2c01),(1,0x212a8)]
        for count in range(5):
            target = self.target()
            for pair in pairs[:count]: self.write(target,*pair)
            self.assertEqual(self.read(target).status,"unresolved")
        for index in range(5):
            for operation in ("omit", "wrong"):
                altered = list(pairs)
                if operation == "omit": del altered[index]
                else: altered[index] = (pairs[index][0], pairs[index][1]^1)
                target = self.target()
                for pair in altered: self.write(target,*pair)
                self.assertEqual(self.read(target).status,"unresolved")
        target = self.target()
        for pair in reversed(pairs): self.write(target,*pair)
        self.assertEqual(self.read(target).status,"unresolved")
        for pair in ((1,0),(320,1),(321,1),(467,1),(447,0)):
            self.setup(target); self.write(target,*pair)
            self.assertEqual(self.read(target).status,"unresolved")

    def test_reset_isolation_disabled_and_unknown_register(self):
        a,b = self.target(),self.target()
        self.setup(a)
        self.assertEqual(self.read(b).status,"unresolved")
        self.assertEqual(self.read(a,469).status,"unresolved")
        self.write(a,0,0x80000)
        self.assertEqual(self.read(a).status,"unresolved")
        self.assertEqual(a.rx_rc_calibration.completions,0)
        self.setup(a); rf.SerialBus({0:a,1:b}).reset()
        self.assertEqual(self.read(a).status,"unresolved")
        disabled = rf.SoftwareMt6177Target(8,0)
        self.setup(disabled)
        self.assertEqual(self.read(disabled).status,"unresolved")

    def test_validation_and_defensive_copy(self):
        cfg = config()
        profile = dict(schema="firmwire.software-rf/v1",name="test",analysis_only=True,
                       assumptions="test",rom_sha256="a"*64,ports={"0":dict(chip_id=8,eco=0)})
        bad = [None,{}, {**cfg,"kind":"mt6177l-rx-rc-analysis/v1"}, {**cfg,"source":"silicon"},
               {**cfg,"reason":" "},{**cfg,"extra":0},
               *({**cfg,"trim6":v} for v in (-1,64,True,"1",1.0))]
        for item in bad:
            with self.assertRaises(ValueError): rf.validate_rx_rc_config(item)
            profile["ports"]["0"]["rx_rc_calibration"] = item
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile,"a"*64)
        profile["ports"]["0"]["rx_rc_calibration"] = cfg
        self.assertEqual(rf.validate_software_rf_profile(profile,"a"*64),profile)
        target = rf.SoftwareMt6177Target(8,0,rx_rc_calibration=cfg)
        cfg["trim6"] = 0
        self.setup(target)
        self.assertEqual(self.read(target).value,23<<14)
        facts = target.facts(); facts["rx_rc_calibration"]["config"]["trim6"] = 0
        self.assertEqual(self.read(target).value,23<<14)
        seed = {"447":dict(value=0,source="analysis-assumption",reason="test")}
        with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8,0,seed,rx_rc_calibration=cfg)
        profile["ports"]["0"]["reset_registers"] = seed
        with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile,"a"*64)


if __name__ == "__main__": unittest.main()
