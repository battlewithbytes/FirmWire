"""Dependency-free tests for the diagnostic RAM observer's safety boundary."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    "mtk_observation", Path(__file__).resolve().parents[1] /
    "firmwire/vendor/mtk/observation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
validate = module.validate_ram_observer


def region(begin=0x1000, end=0x2000, **kwargs):
    fields = dict(forwarded=False, is_special=False, is_symbolic=False, permissions="rw")
    fields.update(kwargs)
    return SimpleNamespace(begin=begin, end=end, data=SimpleNamespace(**fields))


class RamObserverTests(unittest.TestCase):
    def test_pc_markers_default_off_and_exact_lookup(self):
        self.assertEqual(module.validate_pc_markers({}), {})
        self.assertEqual(module.validate_pc_markers({"pc_markers": {"entry": 0x1234}}),
                         {0x1234: "entry"})

    def test_invalid_pc_marker_definitions_fail_closed(self):
        for markers in (None, [], {"bad label": 2}, {"x": True}, {"x": -2},
                        {"x": 3}, {"x": 1 << 32}, {"x": 2, "y": 2},
                        {"x"*65: 2}, {"pc%d" % i: 2*i for i in range(17)}):
            with self.subTest(markers=markers), self.assertRaises(ValueError):
                module.validate_pc_markers({"pc_markers": markers})

    def test_marker_counts_are_context_local_not_task_claims(self):
        first, second = {}, {}
        module.record_pc_marker(first, "entry", 10)
        module.record_pc_marker(first, "entry", 30)
        module.record_pc_marker(second, "entry", 20)
        self.assertEqual(first["pc_markers"]["entry"],
                         dict(hits=2, first_block=10, last_block=30))
        self.assertEqual(second["pc_markers"]["entry"]["hits"], 1)
        self.assertNotIn("task_progress_verified", first)

    def setUp(self):
        self.config = dict(schema="cockpit.mtk-ram-observer/v1", rom_sha256="a" * 64,
                           words=[0x1000, 0x1ffc])

    def check(self, regions=None):
        regions = [region()] if regions is None else regions
        return validate(self.config, "a" * 64,
                        lambda address: [r for r in regions if r.begin <= address < r.end])

    def test_ram_boundaries(self):
        self.assertEqual(self.check(), self.config["words"])

    def test_identity_and_malformed_root(self):
        for config in ([], None, {}, dict(self.config, rom_sha256="b" * 64),
                       dict(self.config, schema="other")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate(config, "a" * 64, lambda _: [region()])

    def test_word_format_and_limit(self):
        for words in ([], None, "4096", [True], [-4], [0x100000000], [0x1001],
                      [0x1000, 0x1000], [0x1000 + i * 4 for i in range(17)]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                self.config["words"] = words
                self.check()

    def test_mmio_special_symbolic_and_rom_refused(self):
        for options in (dict(forwarded=True), dict(is_special=True),
                        dict(is_symbolic=True), dict(emulate=object()),
                        dict(permissions="rx"), dict(permissions="w")):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.check([region(**options)])

    def test_holes_boundaries_and_overlaps_refused(self):
        for regions in ([], [region(end=0x1fff)], [region(), region()],
                        [region(), region(begin=0x1002, end=0x1003, forwarded=True)]):
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                self.check(regions)


if __name__ == "__main__":
    unittest.main()
