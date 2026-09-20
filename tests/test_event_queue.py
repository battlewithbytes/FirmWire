import random
import unittest

from firmwire.hw.event_queue import MaskedEventQueue


class EventQueueTests(unittest.TestCase):
    def test_cancel_selected_events_without_delivering_or_erasing_others(self):
        for banks, width in ((1, 1), (2, 8), (5, 32), (7, 64)):
            with self.subTest(banks=banks, width=width):
                q = MaskedEventQueue(banks, width)
                mask = (1 << width)-1
                for bank in range(banks): q.schedule(bank, mask, 10+bank)
                self.assertEqual(q.cancel(0, 1), 1)
                self.assertEqual(q.cancel(0, 0), 0)
                self.assertEqual(q.take_due(9), [])
                due = q.take_due(100)
                self.assertEqual(len(due), banks*width-1)
                self.assertNotIn(dict(bank=0, bit=0, deadline=10), due)
                self.assertEqual(q.take_due(100), [])

    def test_reschedule_reset_detached_facts_and_validation(self):
        q = MaskedEventQueue(2, 8)
        q.schedule(1, 3, 5)
        q.schedule(1, 1, 8)
        q.facts()["queued"].clear()
        self.assertEqual(q.take_due(5), [dict(bank=1, bit=1, deadline=5)])
        self.assertEqual(q.take_due(7), [])
        self.assertEqual(q.take_due(8), [dict(bank=1, bit=0, deadline=8)])
        for bank, mask in ((-1, 1), (2, 1), (False, 1), (0, True), (0, -1), (0, 256)):
            with self.assertRaises(ValueError): q.cancel(bank, mask)
            with self.assertRaises(ValueError): q.schedule(bank, mask, 1)
        for tick in (-1, True, 2**64, 1.0):
            with self.assertRaises(ValueError): q.schedule(0, 1, tick)
            with self.assertRaises(ValueError): q.take_due(tick)
        for banks, width in ((0, 32), (65, 32), (1, 0), (1, 65), (True, 8)):
            with self.assertRaises(ValueError): MaskedEventQueue(banks, width)
        q.schedule(0, 255, 2**64-1)
        q.reset()
        self.assertEqual(q.take_due(2**64-1), [])
        self.assertEqual(q.facts()["cancel_writes"], 0)

    def test_randomized_against_independent_queue(self):
        rng = random.Random(3401)
        q, expected = MaskedEventQueue(3, 8), {}
        for _ in range(500):
            bank, mask, tick = rng.randrange(3), rng.randrange(256), rng.randrange(100)
            action = rng.randrange(3)
            if action == 0:
                q.schedule(bank, mask, tick)
                expected.update({(bank, bit): tick for bit in range(8) if mask & (1 << bit)})
            elif action == 1:
                removed = sum(1 << bit for bit in range(8) if mask & (1 << bit) and (bank, bit) in expected)
                self.assertEqual(q.cancel(bank, mask), removed)
                expected = {key: value for key, value in expected.items() if key[0] != bank or not mask & (1 << key[1])}
            else:
                due = sorted((value, *key) for key, value in expected.items() if value <= tick)
                self.assertEqual(q.take_due(tick), [dict(deadline=d, bank=b, bit=i) for d, b, i in due])
                expected = {key: value for key, value in expected.items() if value > tick}
