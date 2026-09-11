"""Platform mapping validation, without PANDA or proprietary inputs."""
from enum import Enum
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("boot_slave", Path(__file__).resolve().parents[1] /
                                             "firmwire/vendor/mtk/boot_slave.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Kind(Enum):
    PERIPHERAL = 1
    GENERIC = 2


class MDPERISYS_MISC_Periph:
    pass


class BootSlaveTests(unittest.TestCase):
    def setUp(self):
        self.entry = SimpleNamespace(start=0xa0060000, size=0x2000, ty=Kind.PERIPHERAL,
            kwargs=dict(name="MDPERI_MDPERISYS_MISC_REG", emulate=MDPERISYS_MISC_Periph))
        self.device = dict(kind="mtk-bootslave-analysis-v1", address=self.entry.start, size=0x2000)
        self.topology = SimpleNamespace(cores=2)

    def apply(self, devices, entries=None):
        return module.apply_boot_slave_profile([self.entry] if entries is None else entries,
                                               dict(platform_devices=devices), self.topology)

    def test_default_off(self):
        entries, report = self.apply([])
        self.assertIsNone(report)
        self.assertIs(entries[0], self.entry)

    def test_replacement_preserves_original_and_labels_limits(self):
        entries, report = self.apply([self.device])
        self.assertEqual(entries[0].ty, Kind.GENERIC)
        self.assertNotIn("emulate", entries[0].kwargs)
        self.assertEqual(self.entry.ty, Kind.PERIPHERAL)
        self.assertIn("emulate", self.entry.kwargs)
        self.assertTrue(report["additional_core_release_wired"])
        self.assertFalse(report["power_controller_modeled"])
        self.assertFalse(report["firmware_boot_verified"])

    def test_wrong_map_and_ambiguous_overlap_rejected(self):
        for entries in ([], [self.entry, self.entry]):
            with self.assertRaises(ValueError):
                self.apply([self.device], entries)
        self.entry.kwargs["name"] = "different device"
        with self.assertRaises(ValueError):
            self.apply([self.device])

    def test_invalid_or_unknown_device_fails_closed(self):
        for devices in (None, {}, [self.device]*2, [dict(self.device, kind="unknown")],
                        [dict(self.device, address=True)], [dict(self.device, size=4096)],
                        [dict(self.device, address=0xa0060001)], [dict(self.device, extra=1)]):
            with self.subTest(devices=devices), self.assertRaises(ValueError):
                self.apply(devices)
        self.topology.cores = 1
        with self.assertRaises(ValueError):
            self.apply([self.device])
