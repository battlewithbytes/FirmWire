import struct
import unittest

from firmwire.vendor.mtk.hw.PCCIFPeripheral import SHM_CCIF_Periph, Ringbuf


class RingObserverTests(unittest.TestCase):
    def test_reply_poll_counts_exclude_observer_reads_and_payload(self):
        device = self.device()
        self.assertIsNone(device.control_observation()["rings"][0]["reply_control_reads_since_last_queue"])
        self.assertEqual(device.hw_read(16, 4), 0)
        Ringbuf(device, 0).writePacket(bytes(16))
        self.assertEqual(device.hw_read(16, 4), 32)
        device.hw_read(12, 4)
        device.hw_read(24, 4)  # not reply control
        device.hw_read(280 + 16, 4)  # another queue
        for _ in range(3): device.control_observation()
        facts = device.control_observation()["rings"]
        self.assertEqual(facts[0]["guest_reply_control_reads"], 3)
        self.assertEqual(facts[0]["reply_control_reads_since_last_queue"], 2)
        self.assertEqual(facts[1]["guest_reply_control_reads"], 1)
        self.assertIsNone(facts[1]["reply_control_reads_since_last_queue"])
        Ringbuf(device, 0).writePacket(bytes(16))
        self.assertEqual(device.control_observation()["rings"][0]["reply_control_reads_since_last_queue"], 0)

    def device(self, enabled=True):
        device = object.__new__(SHM_CCIF_Periph)
        device.mem = bytearray(560)
        device.offsets, device.exp_offsets = [0], [280]
        for offset in (0, 280):
            struct.pack_into("<6I", device.mem, offset, 0, 0, 128, 0, 0, 128)
        if enabled:
            device.enable_control_observer()
        return device

    def test_queued_is_not_consumed_and_observation_does_not_write(self):
        device = self.device()
        Ringbuf(device, 0).writePacket(bytes(16))
        original = bytes(device.mem)
        facts = device.control_observation()
        self.assertEqual(facts["rings"][0]["ap_to_md"]["pending_bytes"], 32)
        self.assertEqual(facts["rings"][0]["queued_frames"], 1)
        self.assertEqual(facts["rings"][0]["guest_read_writes"], 0)
        self.assertFalse(facts["application_verified"])
        self.assertFalse(facts["interrupt_delivery_verified"])
        self.assertEqual(bytes(device.mem), original)

    def test_guest_read_updates_all_wrap_positions_and_independent_queues(self):
        for offset in (0, 280):
            for cursor in range(0, 128, 8):
                device = self.device()
                device.write_raw(offset + 12, 4, cursor)
                device.write_raw(offset + 16, 4, cursor)
                Ringbuf(device, offset).writePacket(bytes(16))
                device.hw_write(offset + 12, 4, (cursor + 32) % 128)
                facts = device.control_observation()
                self.assertEqual(facts["recent_events"][-1]["bounded_advance_bytes"], 32)
                active = next(r for r in facts["rings"] if r["offset"] == offset)
                inactive = next(r for r in facts["rings"] if r["offset"] != offset)
                self.assertEqual(active["ap_to_md"]["pending_bytes"], 0)
                self.assertEqual(inactive["guest_read_writes"], 0)

    def test_partial_invalid_reset_and_same_cursor_never_invent_progress(self):
        for cursor, width, expected in ((32, 1, None), (1, 4, None), (64, 4, None), (0, 4, 0)):
            device = self.device()
            Ringbuf(device, 0).writePacket(bytes(16))
            device.hw_write(12, width, cursor)
            self.assertEqual(device.control_observation()["recent_events"][-1]["bounded_advance_bytes"], expected)

    def test_invalid_capacity_and_control_extent_are_reported(self):
        for capacity in (0, 15, 129, 4096):
            device = self.device()
            device.write_raw(20, 4, capacity)
            self.assertFalse(device.control_observation()["rings"][0]["valid"])
        self.assertFalse(self.device().ring_observer.state(559)["valid"])

    def test_default_off_and_bounded_history_and_idempotent_enable(self):
        device = self.device(False)
        Ringbuf(device, 0).writePacket(bytes(16))
        device.hw_write(12, 4, 32)
        self.assertFalse(hasattr(device, "ring_observer"))
        device.enable_control_observer()
        for _ in range(50):
            device.hw_write(12, 4, 32)
        device.enable_control_observer()
        facts = device.control_observation()
        self.assertEqual(len(facts["recent_events"]), 16)
        self.assertEqual(facts["rings"][0]["guest_read_writes"], 50)
        self.assertEqual(facts["rings"][0]["queued_frames"], 0)
