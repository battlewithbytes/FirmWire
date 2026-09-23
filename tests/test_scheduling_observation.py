from types import SimpleNamespace
import unittest
from unittest.mock import patch

from firmwire.emulator.scheduling_observation import SchedulingObserver


class SchedulingObservationTests(unittest.TestCase):
    def panda(self):
        return SimpleNamespace(libpanda_path="test-only",ffi=SimpleNamespace(cast=lambda ty,p:p),libpanda=SimpleNamespace(
            qemu_get_cpu=lambda i: i+10 if i<4 else None,
            cpu_is_stopped=lambda cpu: cpu != 10,
            panda_current_pc=lambda cpu: 0x1000+cpu*4,
            qemu_clock_get_ns=lambda clock: 12345 if clock==1 else None))

    def observer(self,panda,count):
        with patch("firmwire.emulator.scheduling_observation.ctypes.CDLL",return_value=panda.libpanda):
            return SchedulingObserver(panda,count)

    def test_opaque_native_accessors_preserve_stopped_vs_halted_boundary(self):
        panda=self.panda()
        observer=self.observer(panda,4)
        facts=observer.sample(100)
        self.assertEqual(facts["first"]["virtual_ns"],12345)
        self.assertEqual([cpu["stopped"] for cpu in facts["first"]["cpus"]],[False,True,True,True])
        self.assertEqual([cpu["index"] for cpu in facts["first"]["cpus"]],list(range(4)))
        self.assertNotIn("halted",facts["first"]["cpus"][0])
        self.assertFalse(facts["firmware_boot_verified"])
        self.assertTrue(facts["read_only"])

    def test_history_is_bounded_and_first_sample_retained(self):
        observer=self.observer(self.panda(),2)
        for block in range(100): facts=observer.sample(block)
        self.assertEqual(facts["first"]["completed_blocks"],0)
        self.assertEqual(len(facts["recent"]),16)
        self.assertEqual(facts["recent"][0]["completed_blocks"],84)
        self.assertEqual(facts["samples"],100)

    def test_invalid_counts_and_missing_cpu_refused(self):
        for count in (0,True,65,1.5,"4",None,5):
            with self.subTest(count=count),self.assertRaises(ValueError):
                self.observer(self.panda(),count)
