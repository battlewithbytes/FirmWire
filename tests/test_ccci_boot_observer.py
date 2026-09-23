import logging
import pickle
import struct
import unittest

from firmwire.vendor.mtk.hw.ccci_boot_observer import CCCIBootObserver
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph


def hs1():
    data = bytearray(88)
    struct.pack_into("<IIHHI", data, 0, 0xFFFFFFFF, 0, 0, 0, 0x5555FFFF)
    for offset in (16, 84):
        struct.pack_into("<I", data, offset, 0x49434343)
    return data


def response():
    data = bytearray(0xAC)
    struct.pack_into("<IIHHI", data, 0, 0, len(data), 1, 0, 0x5555FFFF)
    for offset in (16, 0xA8):
        struct.pack_into("<I", data, offset, 0x43434349)
    return data


def hs2(reserved=0, channel=0, command=0):
    return struct.pack("<IIHHI", 0xFFFFFFFF, command, channel, 0xFFFF, reserved)


class BootObserverTests(unittest.TestCase):
    def prepared(self):
        observer = CCCIBootObserver()
        observer.hs1(hs1())
        observer.ap_response(response())
        return observer

    def test_ordered_exchange_is_not_full_boot_or_real_ap(self):
        for reserved in (0, 1, 0xFFFFFFFF):
            observer = self.prepared()
            observer.control(hs2(reserved))
            facts = observer.snapshot()
            self.assertTrue(facts["ordered_handshake_observed"])
            self.assertEqual(facts["event_count"], 3)
            for name in ("boot_verified", "real_ap_handshake_verified", "task_progress_verified", "runtime_features_validated"):
                self.assertFalse(facts[name])

    def test_wrong_order_duplicate_and_missing_response(self):
        for events in (("control",), ("hs1", "control"), ("ap_response",),
                       ("hs1", "hs1", "ap_response", "control"),
                       ("hs1", "ap_response", "control", "control")):
            observer = CCCIBootObserver()
            for name in events:
                getattr(observer, name)({"hs1": hs1(), "ap_response": response(), "control": hs2()}[name])
            self.assertFalse(observer.snapshot()["ordered_handshake_observed"])
            self.assertGreater(observer.violation_count, 0)

    def test_malformed_hs1_never_advances(self):
        variants = [hs1()[:n] for n in (0, 15, 16, 87)] + [hs1() + b"x"]
        for offset in (4, 8, 12, 16, 84):
            data = hs1()
            data[offset] ^= 1
            variants.append(data)
        for packet in variants:
            observer = CCCIBootObserver()
            observer.hs1(packet)
            self.assertEqual(observer.state, "awaiting-hs1")
            self.assertEqual(observer.violation_count, 1)

    def test_malformed_response_never_advances(self):
        variants = [response()[:n] for n in (0, 16, 171)] + [response() + b"x"]
        for offset in (0, 4, 8, 12, 16, 168):
            data = response()
            data[offset] ^= 1
            variants.append(data)
        for packet in variants:
            observer = CCCIBootObserver()
            observer.hs1(hs1())
            observer.ap_response(packet)
            self.assertEqual(observer.state, "hs1-query-observed")

    def test_ring_hs1_unknown_command_wrong_channel_truncation_and_payload(self):
        for packet in (hs2(0x5555FFFF), hs2(channel=1), hs2(command=1),
                       hs2()[:15], hs2() + b"x", b""):
            observer = self.prepared()
            observer.control(packet)
            self.assertFalse(observer.snapshot()["ordered_handshake_observed"])

    def test_bounded_sticky_failure_and_snapshot_copy(self):
        observer = self.prepared()
        observer.control(hs2())
        for _ in range(100):
            observer.control(hs2())
        facts = observer.snapshot()
        self.assertEqual(len(facts["recent_events"]), 32)
        self.assertEqual(facts["violation_count"], 100)
        self.assertFalse(facts["ordered_handshake_observed"])
        facts["recent_events"][0]["valid"] = "modified"
        self.assertIs(observer.snapshot()["recent_events"][0]["valid"], False)
        self.assertEqual(pickle.loads(pickle.dumps(observer)).snapshot(), observer.snapshot())

    def device(self, enabled):
        device = object.__new__(PCCIF_Periph)
        device.pccifid = 0
        device.mem = bytearray(4096)
        device.log = logging.getLogger("boot-observer-test")
        device.rchnum = 0
        device.mem[0x100:0x158] = hs1()
        if enabled:
            device.enable_control_observer()
        return device

    def test_real_adapter_preserves_response_memory_and_notification(self):
        off, on = self.device(False), self.device(True)
        for device in (off, on):
            device.handle_SRAM_write()
            device.handleControlPacket(None, hs2())
        self.assertEqual(off.mem, on.mem)
        self.assertEqual(off.rchnum, on.rchnum)
        self.assertEqual(on.rchnum, 1 << 15)
        self.assertTrue(on.control_observation()["boot_protocol"]["ordered_handshake_observed"])
        on.enable_control_observer()
        self.assertEqual(on.boot_observer.event_count, 3)
        self.assertFalse(hasattr(off, "boot_observer"))

    def test_secondary_pccif_does_not_claim_boot_protocol(self):
        device = self.device(False)
        device.pccifid = 1
        device.enable_control_observer()
        self.assertNotIn("boot_protocol", device.control_observation())
