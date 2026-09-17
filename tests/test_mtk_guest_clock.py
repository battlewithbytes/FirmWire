"""Deterministic clock/ordering properties, independent of any modem image."""
import unittest
from test_mtk_rf_serial import load
from test_mtk_hwpor import hwpor

clock_module = load("rf_guest_clock_unit", "guest_clock.py")


class GuestClockTests(unittest.TestCase):
    def test_every_old_batch_phase_has_the_same_relative_event_deadline(self):
        for phase in range(1024):
            blocks = [0]
            clock = clock_module.RfSequencerClock(lambda:blocks[0])
            writes = []
            seq = hwpor.HwporSequencer(hwpor.HwporLayout(0x4000,0x8000),
                                      lambda w: writes.append(w.word) is None)
            clock.attach(seq)
            blocks[0] = phase
            clock.synchronize()
            for off,val in ((0x4000,4),(0x4020,380*75),(0x4024,3<<16|3),
                            (0x8018,0xad21485),(0x801c,0),(0x4004,3)):
                seq.write(off,val)
            for _ in range(3): self.assertEqual(clock.synchronize(),phase)
            self.assertEqual(writes,[])
            blocks[0] += 379
            clock.synchronize()
            self.assertEqual(writes,[])
            blocks[0] += 1
            self.assertEqual(clock.synchronize(),phase+380)
            self.assertEqual(writes,[0xad21485])
            clock.synchronize()
            self.assertEqual(seq.completed_writes,1)

    def test_unknown_backend_stays_pending_even_when_time_moves(self):
        blocks = [0]
        c = clock_module.RfSequencerClock(lambda:blocks[0])
        s = hwpor.HwporSequencer(hwpor.HwporLayout(0x4000,0x8000))
        c.attach(s)
        for off,val in ((0x4000,1),(0x4010,0),(0x4014,0),(0x8000,123),(0x8004,0),(0x4004,3)):
            s.write(off,val)
        c.synchronize()
        for blocks[0] in (1,1024,1000000): c.synchronize()
        self.assertEqual(s.completed_writes,0)
        self.assertIsNotNone(s.pending)
        self.assertEqual(len(s.log),1)

    def test_invalid_sources_rebinding_and_time_reversal_fail(self):
        for source in (None,1):
            with self.assertRaises(ValueError): clock_module.RfSequencerClock(source)
        blocks = [0]
        c = clock_module.RfSequencerClock(lambda:blocks[0])
        s = hwpor.HwporSequencer(hwpor.HwporLayout(0x4000,0x8000))
        c.attach(s)
        with self.assertRaises(ValueError): c.attach(s)
        for value in (-1,True,1.5):
            blocks[0] = value
            with self.assertRaises(ValueError): c.synchronize()
        blocks[0] = 100; c.synchronize()
        blocks[0] = 99
        with self.assertRaises(ValueError): c.synchronize()
        with self.assertRaises(ValueError): c.attach(s)

    def test_no_software_rf_leaves_legacy_boots_unchanged(self):
        self.assertIsNone(clock_module.bind_rf_clock({}))


if __name__ == "__main__": unittest.main()
