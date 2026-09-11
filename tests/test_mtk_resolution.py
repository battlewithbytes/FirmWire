"""Dependency-free resolver tests; debug information is deliberately withheld."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("mtk_resolution", Path(__file__).resolve().parents[1] /
                                            "firmwire/vendor/mtk/resolution.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.rom = bytes(range(128))
        self.base = 0x90000000

    def resolver(self, debug=None, rom=None):
        return module.Resolver(rom or self.rom, self.base, "MT6768", debug)

    def profile(self, resolver, entries):
        return {"schema": module.PROFILE_SCHEMA, "arch": "mipsel", "soc": "MT6768",
                "rom_base": self.base, "rom_sha256": resolver.sha256, "symbols": entries}

    def entry(self, name="stack_init_comp_info"):
        return {"name": name, "address": self.base + 16, "size": 32,
                "expected_hex": self.rom[16:32].hex(), "evidence": "synthetic known function"}

    def test_no_debug_native_is_location_ready_not_booted(self):
        resolver = self.resolver()
        native = resolver.capabilities("native")
        self.assertTrue(native["startup_locations_ready"])
        self.assertFalse(native["cpu_execution_observed"])
        self.assertFalse(native["task_progress_verified"])
        self.assertFalse(resolver.capabilities("rehosted")["startup_locations_ready"])

    def test_known_symbols_withheld_and_recovered_from_signature(self):
        known = self.resolver({"stack_init_comp_info": (self.base + 16, 32)})
        stripped = self.resolver()
        entry = self.entry()
        del entry["address"], entry["expected_hex"]
        entry["pattern"] = self.rom[16:40].hex()
        stripped.apply_profile(self.profile(stripped, [entry]))
        self.assertEqual(known.symbols, stripped.symbols)
        self.assertEqual(known.sizes, stripped.sizes)
        self.assertEqual(stripped.evidence[entry["name"]]["source"], "unique-rom-signature")

    def test_profiles_bind_exact_image_architecture_and_platform(self):
        resolver = self.resolver()
        original = self.profile(resolver, [self.entry()])
        for field, value in (("rom_sha256", "0" * 64), ("soc", "MT6765"),
                             ("arch", "arm"), ("rom_base", 0), ("schema", "unknown")):
            profile = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                resolver.apply_profile(profile)
        self.assertFalse(resolver.symbols)

    def test_bad_witness_bounds_and_duplicate_rejected_atomically(self):
        for changed in ({"expected_hex": "00" * 16}, {"size": 1000}, {"address": self.base - 2},
                        {"evidence": ""}, {"size": True}):
            resolver = self.resolver()
            bad = dict(self.entry("bad"), **changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                resolver.apply_profile(self.profile(resolver, [self.entry(), bad]))
            self.assertFalse(resolver.symbols)
        resolver = self.resolver()
        with self.assertRaises(ValueError):
            resolver.apply_profile(self.profile(resolver, [self.entry(), self.entry()]))

    def test_ambiguous_and_weak_rom_patterns_rejected(self):
        for rom, pattern in ((self.rom + self.rom, self.rom[16:40].hex()),
                             (self.rom, "10111213"), (self.rom, "??" * 16)):
            resolver = self.resolver(rom=rom)
            entry = {"name": "test", "size": 16, "pattern": pattern, "evidence": "test"}
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                resolver.apply_profile(self.profile(resolver, [entry]))

    def test_no_global_fallback_for_unresolved_containing_function(self):
        resolver = self.resolver()
        resolver.scoped_patterns({"hook": {"pattern": "10111213", "within": "missing"}})
        self.assertNotIn("hook", resolver.symbols)
        self.assertIn("missing containing function", resolver.unresolved["hook"])

    def test_scoped_unique_and_ambiguous_patterns(self):
        resolver = self.resolver({"parent": (self.base + 16, 32)})
        resolver.scoped_patterns({"hook": {"pattern": "18191a1b", "within": "parent"}})
        self.assertEqual(resolver.symbols["hook"], self.base + 24)
        rom = b"abcdabcd" + bytes(32)
        resolver = self.resolver({"parent": (self.base, len(rom))}, rom=rom)
        resolver.scoped_patterns({"hook": {"pattern": "61626364", "within": "parent"}})
        self.assertEqual(resolver.unresolved["hook"], "ambiguous pattern")

    def test_optional_hooks_not_required_for_rehosted_preflight(self):
        names = {n for group in module.REHOSTED_GROUPS.values() for n in group}
        resolver = self.resolver({n: (self.base + 16, 32) for n in names})
        report = resolver.capabilities("rehosted")
        self.assertTrue(report["startup_locations_ready"])
        self.assertFalse(report["capabilities"]["named_trace_logging"]["available"])
        self.assertFalse(report["capabilities"]["named_trace_logging"]["enabled"])

    def test_conflict_and_invalid_offsets_refused(self):
        resolver = self.resolver({"stack_init_comp_info": (self.base + 32, 32)})
        with self.assertRaises(ValueError):
            resolver.apply_profile(self.profile(resolver, [self.entry()]))
        resolver = self.resolver()
        for offset in (-1, 1000):
            entry = {"name": "test", "size": 16, "pattern": self.rom[16:40].hex(),
                     "evidence": "test", "offset": offset}
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                resolver.apply_profile(self.profile(resolver, [entry]))


if __name__ == "__main__":
    unittest.main()
