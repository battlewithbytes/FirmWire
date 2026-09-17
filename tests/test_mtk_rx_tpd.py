"""Independent digital protocol tests; no firmware PC/table patches."""
import unittest
from test_mtk_rf_serial import rf, bsi


# Reviewed consumer words, deliberately separate from the implementation list.
PREFIX = (0x14000001,0x14100001,0x14208051,0x144098b1,0x14600880,
          0x18f03d7a,0x19003d7a,0x1ef00002,0x1f41b780,0x1f51b780,
          0x08000001,0x08229276,0x083276a9,0x0b304b0e,0x19d007f8,0x19e007f8)


def config(a=5,b=11):
    return dict(kind="mt6177m-rx-tpd-analysis/v1",source="analysis-assumption",
                reason="test trims, not analog measurements",cw423_trim4=a,cw429_trim4=b)


class RxTpdTests(unittest.TestCase):
    def target(self,a=5,b=11,seeds=None):
        return rf.SoftwareMt6177Target(8,0,reset_registers=seeds,rx_tpd_calibration=config(a,b))

    def write(self,t,word):
        return t.exchange(bsi.SerialCommand(1,0,0,False,False,(word,0),(31,0)))

    def read(self,t,address=423):
        return t.exchange(bsi.SerialCommand(1,0,0,True,False,(0x400|address,0),(0,0)))

    def words(self,a=0xa5c00,b=0x35c00):
        return PREFIX+(0x1d500096|a,0x1d800096|b,0x001212a8,0x00600414)

    def setup(self,t,a=0xa5c00,b=0x35c00):
        for word in self.words(a,b): self.write(t,word)

    def test_all_result_pairs_and_retained_backup_bits(self):
        for a in range(16):
            for b in range(16):
                t = self.target(a,b)
                before = t.facts()
                self.assertEqual(self.read(t).status,"unresolved")
                self.assertEqual(t.facts(),before)
                self.setup(t)
                self.write(t,367<<20|0x12345)
                self.assertEqual(self.read(t,423).value,a<<11)
                self.assertEqual(self.read(t,429).value,b<<11)
                self.assertEqual(self.read(t,469).value,0xa5c96)
                self.assertEqual(self.read(t,472).value,0x35c96)
                self.write(t,0x00600384)
                self.assertEqual(self.read(t,423).status,"unresolved")
                self.assertEqual(self.read(t,429).status,"unresolved")
                for address,upper in ((469,0xa5c00),(472,0x35c00)):
                    backup = self.read(t,address).value
                    self.write(t,address<<20 | (backup&0xffc00))
                    self.assertEqual(self.read(t,address).value,upper)
                self.assertEqual(t.facts()["rx_tpd_calibration"]["reads"],{"423":1,"429":1})
                self.assertFalse(t.facts()["rx_tpd_calibration"]["analog_calibration_verified"])

    def test_every_setup_step_required_and_any_relevant_write_invalidates(self):
        words = self.words()
        for index in range(len(words)):
            for operation in ("omit","wrong","partial"):
                altered = list(words)
                if operation == "omit": del altered[index]
                elif operation == "wrong": altered[index] ^= 1
                else: altered = altered[:index]
                t = self.target()
                for word in altered: self.write(t,word)
                self.assertEqual(self.read(t).status,"unresolved",(index,operation))
        t = self.target()
        for word in reversed(words): self.write(t,word)
        self.assertEqual(self.read(t).status,"unresolved")
        for word in words+(423<<20,429<<20,0x00600384):
            self.setup(t); self.write(t,word)
            self.assertEqual(self.read(t).status,"unresolved")

    def test_arbitrary_preserved_upper_bits_not_hardcoded_to_fixture(self):
        for upper in (0,0x400,0x55000,0xffc00):
            t = self.target()
            self.setup(t,upper,0xffc00^upper)
            self.assertEqual(self.read(t).value,5<<11)
            self.assertEqual(self.read(t,469).value,upper|0x96)

    def test_seeds_are_independent_from_results_reset_and_isolation(self):
        seeds = {str(a):dict(value=v,source="analysis-assumption",reason="unknown reset, test seed")
                 for a,v in ((469,0xa5d23),(472,0x35e45))}
        t,other = self.target(seeds=seeds),self.target()
        self.assertEqual(self.read(t,469).value,0xa5d23)
        self.assertEqual(self.read(t).status,"unresolved")
        self.assertEqual(self.read(other,469).status,"unresolved")
        self.setup(t)
        self.assertEqual(self.read(other).status,"unresolved")
        self.assertEqual(self.read(t,500).value,0x1b780)  # actual guest write, not blanket result
        self.assertEqual(self.read(t,600).status,"unresolved")
        self.write(t,0x80000)
        self.assertEqual(self.read(t).status,"unresolved")
        self.assertEqual(self.read(t,469).value,0xa5d23)
        self.assertEqual(t.rx_tpd_calibration.completions,0)
        self.setup(t); rf.SerialBus({0:t,1:other}).reset()
        self.assertEqual(self.read(t).status,"unresolved")
        disabled = rf.SoftwareMt6177Target(8,0,reset_registers=seeds)
        self.setup(disabled)
        self.assertEqual(self.read(disabled).status,"unresolved")

    def test_validation_and_copying(self):
        cfg = config()
        profile = dict(schema="firmwire.software-rf/v1",name="test",analysis_only=True,
                       assumptions="test",rom_sha256="a"*64,ports={"0":dict(chip_id=8,eco=0)})
        bad = [None,{}, {**cfg,"kind":"mt6177l-rx-tpd-analysis/v1"},{**cfg,"source":"silicon"},
               {**cfg,"reason":" "},{**cfg,"extra":0},
               *({**cfg,k:v} for k in ("cw423_trim4","cw429_trim4") for v in (-1,16,True,"1",1.0))]
        for item in bad:
            with self.assertRaises(ValueError): rf.validate_rx_tpd_config(item)
            profile["ports"]["0"]["rx_tpd_calibration"] = item
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile,"a"*64)
        profile["ports"]["0"]["rx_tpd_calibration"] = cfg
        self.assertEqual(rf.validate_software_rf_profile(profile,"a"*64),profile)
        t = rf.SoftwareMt6177Target(8,0,rx_tpd_calibration=cfg)
        cfg["cw423_trim4"] = 0
        self.setup(t)
        self.assertEqual(self.read(t).value,5<<11)
        facts=t.facts(); facts["rx_tpd_calibration"]["reads"]["423"]=100
        self.assertEqual(t.facts()["rx_tpd_calibration"]["reads"]["423"],1)
        for a in (423,429):
            seed={str(a):dict(value=0,source="analysis-assumption",reason="test")}
            with self.assertRaises(ValueError): rf.SoftwareMt6177Target(8,0,seed,rx_tpd_calibration=cfg)
            profile["ports"]["0"]["reset_registers"]=seed
            with self.assertRaises(ValueError): rf.validate_software_rf_profile(profile,"a"*64)


if __name__ == "__main__": unittest.main()
