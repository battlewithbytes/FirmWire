import json
import logging
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from firmwire.vendor.mtk.hw.ccci_mailbox import MailboxDispatcher, MailboxMessage, MailboxResult, MailboxEndpoint
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph, CCIF_PKG_HEADER, CCIF_PKG_FOOTER


class Endpoint(MailboxEndpoint):
    def __init__(self, response=None):
        self.response = response
        self.calls = 0

    def receive(self, message):
        self.calls += 1
        return MailboxResult("test-explicit-disposition", self.response)


class MailboxTransportTests(unittest.TestCase):
    def device(self, endpoint):
        device = object.__new__(PCCIF_Periph)
        device.log = logging.getLogger("mailbox-transport-test")
        device.mailbox_dispatcher = MailboxDispatcher([(2, 7, endpoint)])
        device.closed_ports = Mock()
        device.handleControlPacket = Mock()
        return device

    def test_no_reply_and_explicit_reply_share_transport_but_not_handler(self):
        for response in (None, MailboxMessage(3, 8, 42)):
            device = self.device(Endpoint(response))
            ring = Mock()
            ring.writePacket.return_value = True
            request = MailboxMessage(2, 7, 0xdeadbeef, 0x1234).encode()
            with self.assertLogs(device.log, level="INFO") as logs:
                device.dispatch_ccci_packet(ring, request, {"ring_index": 4})
            facts = json.loads(logs.output[0].split("metadata=", 1)[1])
            self.assertEqual(facts["response_queued"], response is not None)
            self.assertEqual(facts["mailbox_message_id"], 7)
            self.assertFalse(facts["peer_connected"])
            self.assertNotIn(str(0xdeadbeef), logs.output[0])
            if response is None: ring.writePacket.assert_not_called()
            else: ring.writePacket.assert_called_once_with(response.encode())
            device.closed_ports.receive.assert_not_called()

    def test_unknown_id_does_not_fall_through_to_closed_port_or_reply(self):
        device = self.device(Endpoint())
        ring = Mock()
        with self.assertLogs(device.log, level="ERROR"), self.assertRaises(NotImplementedError):
            device.dispatch_ccci_packet(ring, MailboxMessage(2, 99).encode(), {})
        ring.writePacket.assert_not_called()
        device.closed_ports.receive.assert_not_called()

    def test_failed_queue_does_not_report_success(self):
        device = self.device(Endpoint(MailboxMessage(3, 8)))
        ring = Mock()
        ring.writePacket.return_value = False
        with self.assertLogs(device.log, level="ERROR"), self.assertRaises(RuntimeError):
            device.dispatch_ccci_packet(ring, MailboxMessage(2, 7).encode(), {})

    def test_existing_control_route_remains_separate(self):
        device = self.device(Endpoint())
        ring, packet = Mock(), MailboxMessage(0, 7).encode()
        device.dispatch_ccci_packet(ring, packet, {})
        device.handleControlPacket.assert_called_once_with(ring, packet)
        ring.writePacket.assert_not_called()

    def test_real_ring_reply_bytes_and_cursors_without_invented_irq(self):
        request = MailboxMessage(2, 7, 0xdeadbeef, 0x8123).encode()
        response = MailboxMessage(3, 8, 42)
        capacity = 128
        for start in range(0, capacity, 8):
            mem = bytearray(32 + 2*capacity)
            struct.pack_into("<6I", mem, 8, 0, 32, capacity, start, start, capacity)
            struct.pack_into("<II", mem, 32, CCIF_PKG_HEADER, 16)
            mem[40:56] = request
            struct.pack_into("<II", mem, 56, CCIF_PKG_FOOTER, CCIF_PKG_FOOTER)
            parent = SimpleNamespace(mem=mem, offsets=[8],
                read_raw=lambda offset, size: struct.unpack_from("<I", mem, offset)[0],
                write_raw=lambda offset, size, value: struct.pack_into("<I", mem, offset, value))
            device = self.device(Endpoint(response))
            device.pccifid, device.ringbuffer, device.rchnum = 0, parent, 0
            with self.assertLogs(device.log, level="INFO"):
                self.assertTrue(device.hw_write(0xc, 4, 0))
            self.assertEqual(parent.read_raw(8, 4), 32)
            self.assertEqual(parent.read_raw(24, 4), (start + 32) % capacity)
            frame = bytes(mem[160 + (start+i) % capacity] for i in range(32))
            expected = (struct.pack("<II", CCIF_PKG_HEADER, 16) + response.encode()
                        + struct.pack("<II", CCIF_PKG_FOOTER, CCIF_PKG_FOOTER))
            self.assertEqual(frame, expected)
            self.assertEqual(mem[40:56], request)
            self.assertEqual(device.rchnum, 0)
