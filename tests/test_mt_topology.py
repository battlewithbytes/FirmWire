"""Topology validation runs without firmware, avatar, or PANDA installed."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("mt_topology", Path(__file__).resolve().parents[1] /
                                             "firmwire/emulator/mt_topology.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TopologyTests(unittest.TestCase):
    def test_four_vpes_are_not_two_cores(self):
        one = module.MipsMTTopology(1, 4, 4)
        two = module.MipsMTTopology(2, 2, 4)
        self.assertEqual(one.cpu_count, two.cpu_count)
        self.assertNotEqual(one.engine_config(), two.engine_config())

    def test_unknown_invalid_and_unsupported_counts_fail(self):
        for values in ((None, 2, 4), (True, 2, 4), (1, "2", 4), (0, 2, 4),
                       (1, 4, 2), (1, 5, 5), (1, 2, 6), (5, 2, 4)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                module.MipsMTTopology(*values)

    def test_profile_is_identity_bound_and_preserves_provenance(self):
        profile = dict(schema="cockpit.mips-mt-startup/v1", rom_sha256="a"*64,
                       cpu_model="test-cpu", confidence="experimental", evidence=["synthetic test"],
                       topology=dict(cores=1, vpes_per_core=4, tcs_per_core=4))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(profile))
            topology, report = module.load_profile(path, "a"*64, "test-cpu")
            self.assertEqual(topology.cpu_count, 4)
            self.assertEqual(report["profile"]["evidence"], ["synthetic test"])
            self.assertFalse(report["additional_core_release_wired"])
            for rom, cpu in (("b"*64, "test-cpu"), ("a"*64, "wrong-cpu")):
                with self.assertRaises(ValueError):
                    module.load_profile(path, rom, cpu)
            for change in (dict(confidence=None), dict(evidence=[]), dict(topology=dict(cores=1))):
                path.write_text(json.dumps(dict(profile, **change)))
                with self.assertRaises(ValueError):
                    module.load_profile(path, "a"*64, "test-cpu")


if __name__ == "__main__":
    unittest.main()
