"""Passive evidence for the existing CCCI SRAM-query/ring-HS2 analysis peer.

This is a versioned transport contract, not a modem-family detector or an AP
FSM replacement. No guest memory, interrupt or response is changed here.
"""
import struct
from collections import deque


class CCCIBootObserver:
    ABI = "ccci-sram-query-ap-v2.1-ring-hs2/v1"
    CHECK_ID = 0x5555FFFF

    def __init__(self):
        self.state = "awaiting-hs1"
        self.events = deque(maxlen=32)
        self.event_count = 0
        self.violation_count = 0

    def _record(self, event, valid, **facts):
        self.event_count += 1
        if not valid:
            self.violation_count += 1
        self.events.append(dict(event=event, valid=valid, ordinal=self.event_count,
                                state=self.state, **facts))

    def hs1(self, prefix):
        # Only the 88-byte header/query prefix, not reserved SRAM or payloads.
        valid = len(prefix) == 88
        if valid:
            _, command, channel, _, reserved = struct.unpack_from("<IIHHI", prefix)
            head, tail = struct.unpack_from("<I", prefix, 16)[0], struct.unpack_from("<I", prefix, 84)[0]
            valid = (command == 0 and channel == 0 and reserved == self.CHECK_ID
                     and head == tail == 0x49434343)
        valid = valid and self.state == "awaiting-hs1"
        if valid:
            self.state = "hs1-query-observed"
        self._record("hs1-query", valid)

    def ap_response(self, packet):
        valid = len(packet) == 0xAC
        if valid:
            data0, size, channel, _, reserved = struct.unpack_from("<IIHHI", packet)
            head = struct.unpack_from("<I", packet, 16)[0]
            tail = struct.unpack_from("<I", packet, 0xA8)[0]
            valid = (data0 == 0 and size == len(packet) and channel == 1
                     and reserved == self.CHECK_ID and head == tail == 0x43434349)
        valid = valid and self.state == "hs1-query-observed"
        if valid:
            self.state = "awaiting-hs2"
        self._record("synthetic-ap-response-written", valid)

    def control(self, packet):
        facts = {"size": len(packet)}
        valid = len(packet) == 16
        if len(packet) >= 16:
            data0, command, channel, sequence, reserved = struct.unpack_from("<IIHHI", packet)
            facts.update(data0=data0, command=command, channel=channel,
                         sequence=sequence, reserved=reserved)
            # The AP driver distinguishes HS1 from HS2 by reserved, not count.
            # Do not impose sequence/data0 semantics not used by that driver.
            valid = valid and command == 0 and channel == 0 and reserved != self.CHECK_ID
        valid = valid and self.state == "awaiting-hs2"
        if valid:
            self.state = "hs2-observed"
        self._record("ring-control", valid, **facts)

    def snapshot(self):
        return dict(schema="firmwire.ccci-boot-observation/v1", abi=self.ABI,
                    state=self.state, event_count=self.event_count,
                    violation_count=self.violation_count,
                    recent_events=[dict(event) for event in self.events],
                    ordered_handshake_observed=(self.state == "hs2-observed" and self.violation_count == 0),
                    peer="synthetic-analysis", runtime_features_validated=False,
                    boot_verified=False, real_ap_handshake_verified=False,
                    task_progress_verified=False)
