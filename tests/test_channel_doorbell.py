import unittest
from firmwire.hw.channel_doorbell import ChannelDoorbell


class ChannelDoorbellTests(unittest.TestCase):
    def test_independent_bits_coalesce_until_ack_and_can_reassert(self):
        changes = []
        doorbell = ChannelDoorbell(32, changes.append)
        for channel in (0, 31, 0): doorbell.notify(channel)
        self.assertEqual(changes, [1, 0x80000001])
        self.assertEqual(doorbell.acknowledge(1), 1)
        self.assertEqual(doorbell.pending, 0x80000000)
        self.assertEqual(doorbell.acknowledge(1), 0)
        doorbell.notify(0)
        self.assertEqual(doorbell.acknowledge(0xffffffff), 0x80000001)
        facts = doorbell.snapshot()
        self.assertEqual(facts["notifications"][0], 3)
        self.assertEqual(facts["coalesced"][0], 1)
        self.assertEqual(facts["acknowledged"][0], 2)
        self.assertEqual(changes[-1], 0)

    def test_invalid_inputs_leave_state_and_snapshots_detached(self):
        for channels in (0, 33, True):
            with self.assertRaises(ValueError): ChannelDoorbell(channels)
        doorbell = ChannelDoorbell(4)
        before = doorbell.snapshot()
        for value in (-1, 4, True, "1"):
            with self.assertRaises(ValueError): doorbell.notify(value)
        for value in (-1, 2**32, True):
            with self.assertRaises(ValueError): doorbell.acknowledge(value)
        self.assertEqual(doorbell.snapshot(), before)
        before["notifications"][0] = 99
        self.assertEqual(doorbell.snapshot()["notifications"][0], 0)
