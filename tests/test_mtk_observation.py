"""Dependency-free tests for the diagnostic RAM observer's safety boundary."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
    def test_tb_exit_policy_rejects_early_exits_and_unknown_codes(self):
        self.assertTrue(module.normal_tb_exit(0))
        self.assertTrue(module.normal_tb_exit(1))
        self.assertFalse(module.normal_tb_exit(2))
        self.assertFalse(module.normal_tb_exit(3))
        for code in (True, -1, 4, None, "0"):
            with self.assertRaises(ValueError): module.normal_tb_exit(code)

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
        self.assertTrue(module.record_pc_marker(first, "entry", 10))
        self.assertFalse(module.record_pc_marker(first, "entry", 30))
        self.assertTrue(module.record_pc_marker(second, "entry", 20))
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

    def test_byte_windows_read_exact_physical_spans_and_preserve_input(self):
        config = dict(self.config, byte_windows={"runtime": {"address": 0x1100, "size": 256}})
        reader = Mock(side_effect=lambda address, size: bytes(range(size)))
        sampler = module.RamObservation(config, "a" * 64, lambda _: [region()], reader)
        config["byte_windows"]["runtime"]["address"] = 0
        result = sampler.sample(17)
        self.assertEqual(result["byte_windows"]["runtime"],
                         {"address": 0x1100, "size": 256, "hex": bytes(range(256)).hex()})
        self.assertEqual(reader.call_args.args, (0x1100, 256))
        reader.side_effect = lambda address, size: bytes(4)
        with self.assertRaisesRegex(ValueError, "short byte-window"):
            sampler.sample(18)

    def test_bad_byte_windows_fail_before_any_read(self):
        cases = [None, [], {"a": {}}, {"bad label": {"address": 0x1100, "size": 4}},
                 {str(i): {"address": 0x1100 + 4*i, "size": 4} for i in range(5)},
                 {"a": {"address": 0x1100, "size": 8}, "b": {"address": 0x1104, "size": 4}}]
        cases += [{"a": {"address": address, "size": size}} for address, size in
                  ((True, 4), (0x1100, True), (-4, 4), (0x1101, 4), (0x1100, 0),
                   (0x1100, 257), (0x1100, 6), (0xfffffffc, 8), (0x1ffc, 8))]
        reader = Mock()
        for windows in cases:
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                module.RamObservation(dict(self.config, byte_windows=windows), "a"*64,
                                      lambda _: [region()], reader)
        reader.assert_not_called()

    def test_window_rejects_mmio_hole_and_single_byte_overlap(self):
        reader = Mock()
        config = dict(self.config, byte_windows={"a": {"address": 0x1100, "size": 64}})
        for ranges in ([region(), region(begin=0x1121, end=0x1122, forwarded=True)],
                       [region(end=0x1120), region(begin=0x1124)],
                       [region(end=0x1100), region(begin=0x1100, end=0x1140, permissions="rx"),
                        region(begin=0x1140)]):
            with self.assertRaises(ValueError):
                module.RamObservation(config, "a"*64,
                    lambda address: [r for r in ranges if r.begin <= address < r.end], reader)
        reader.assert_not_called()

    def test_sampler_is_relocatable_and_image_bound(self):
        # Unrelated synthetic images/layouts; no Lagos or vendor source data.
        for identity, base in (("a" * 64, 0x1000), ("b" * 64, 0x7000)):
            config = dict(self.config, rom_sha256=identity, words=[base, base + 12])
            reader = Mock(side_effect=[b"\x01\x02\x03\x04", b"\x05\0\0\0"])
            sampler = module.RamObservation(config, identity,
                lambda _: [region(base, base + 16)], reader)
            self.assertEqual(sampler.sample(123), {"completed_blocks": 123,
                "words": {hex(base): 0x04030201, hex(base + 12): 5}})
            self.assertEqual([call.args for call in reader.call_args_list],
                             [(base, 4), (base + 12, 4)])
            with self.assertRaises(ValueError):
                module.RamObservation(config, "c" * 64, lambda _: [region()], reader)
            self.assertEqual(reader.call_count, 2)

    def test_sampler_never_reads_unvalidated_mmio_or_holes(self):
        reader = Mock()
        for ranges in ([], [region(forwarded=True)], [region(), region()]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                module.RamObservation(self.config, "a" * 64, lambda _: ranges, reader)
        reader.assert_not_called()

    def test_sampler_snapshots_do_not_alias_and_short_reads_fail(self):
        reader = Mock(return_value=b"\1\0\0\0")
        sampler = module.RamObservation(self.config, "a" * 64, lambda _: [region()], reader)
        first = sampler.sample(1)
        first["words"]["0x1000"] = 99
        self.assertEqual(sampler.sample(2)["words"]["0x1000"], 1)
        reader.return_value = b"\0"
        with self.assertRaises(ValueError):
            sampler.sample(3)

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
