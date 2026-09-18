import json
import logging
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from firmwire.vendor.mtk.hw.ccci_metadata import packet_metadata
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph, CCIF_PKG_HEADER, CCIF_PKG_FOOTER


class CCCIMetadataTests(unittest.TestCase):
    def test_real_ring_consumption_is_unchanged_by_metadata(self):
        mem = bytearray(512)
        struct.pack_into("<III",mem,8,0,40,256)
        packet = struct.pack("<IIHHI",0,24,0x88,0,0)+b"private!"
        struct.pack_into("<II",mem,32,CCIF_PKG_HEADER,len(packet))
        mem[40:64] = packet
        struct.pack_into("<II",mem,64,CCIF_PKG_FOOTER,CCIF_PKG_FOOTER)
        parent = SimpleNamespace(mem=mem,offsets=[8],
            read_raw=lambda offset,size: struct.unpack_from("<I",mem,offset)[0],
            write_raw=lambda offset,size,value: struct.pack_into("<I",mem,offset,value))
        device = object.__new__(PCCIF_Periph)
        device.pccifid, device.ringbuffer = 0, parent
        device.log = logging.getLogger("ccci-real-ring-test")
        with self.assertLogs(device.log,level="ERROR") as logs, self.assertRaises(AssertionError):
            device.hw_write(0xc,4,0)
        facts = json.loads(logs.output[0].split("metadata=",1)[1])
        self.assertEqual((facts["read"],facts["write"],facts["capacity"]),(0,40,256))
        self.assertEqual(parent.read_raw(8,4),40)
        self.assertEqual(mem[40:64],packet)
        self.assertNotIn("private!",logs.output[0])

    def test_stream_mailbox_unknown_and_truncated_without_payload(self):
        secret = b"must-not-log-this-payload"
        for data0, kind in ((0,"stream"),(0xffffffff,"mailbox"),(17,"unclassified")):
            packet = struct.pack("<IIHHI",data0,16+len(secret),0x88,0xabcd,0x12345678)+secret
            facts = packet_metadata(packet)
            self.assertEqual((facts["header_kind"],facts["channel"],facts["channel_aux"]),(kind,0x88,0xabcd))
            self.assertEqual(facts["stream_length_matches"],True if data0 == 0 else None)
            self.assertNotIn(secret.decode(),json.dumps(facts))
            self.assertNotIn("reserved",facts)
        for length in range(16):
            facts = packet_metadata(b"x"*length)
            self.assertFalse(facts["header_complete"])
            self.assertIsNone(facts["channel"])
        self.assertFalse(packet_metadata(struct.pack("<IIII",0,17,7,0))["stream_length_matches"])

    def test_dispatch_remains_strict_and_records_unknown_metadata(self):
        device = object.__new__(PCCIF_Periph)
        device.pccifid = 0
        device.log = logging.getLogger("ccci-metadata-test")
        device.ringbuffer = SimpleNamespace(offsets=[8],read_raw=lambda offset,size: {8:0,12:40,16:256}[offset])
        for channel, handler in ((0x20,"handleRPCPacket"),(0xe,"handleFSPacket"),(0,"handleControlPacket"),(0x88,None)):
            for name in ("handleRPCPacket","handleFSPacket","handleControlPacket"):
                setattr(device,name,Mock())
            packet = struct.pack("<IIHHI",0,24,channel,0,0)+b"secret!!"
            with patch("firmwire.vendor.mtk.hw.PCCIFPeripheral.Ringbuf") as cls:
                cls.return_value.offset = 8
                cls.return_value.readPacket.return_value = packet
                if handler is None:
                    with self.assertLogs(device.log,level="ERROR") as logs, self.assertRaises(AssertionError):
                        device.hw_write(0xc,4,0)
                    facts = json.loads(logs.output[0].split("metadata=",1)[1])
                    self.assertEqual((facts["ring_index"],facts["channel"],facts["packet_bytes"]),(0,0x88,24))
                    self.assertTrue(facts["header_complete"])
                    self.assertNotIn("secret",logs.output[0])
                else:
                    device.hw_write(0xc,4,0)
                    getattr(device,handler).assert_called_once_with(cls.return_value,packet)
                cls.return_value.writePacket.assert_not_called()

    def test_short_header_never_dispatches_or_sends_response(self):
        device = object.__new__(PCCIF_Periph)
        device.pccifid, device.log = 0, Mock()
        device.ringbuffer = SimpleNamespace(offsets=[8],read_raw=lambda offset,size: 0)
        device.handleControlPacket = Mock()
        with patch("firmwire.vendor.mtk.hw.PCCIFPeripheral.Ringbuf") as cls:
            cls.return_value.offset = 8
            cls.return_value.readPacket.return_value = b"\0"*10
            with self.assertRaisesRegex(ValueError,"Truncated CCCI"):
                device.hw_write(0xc,4,0)
            device.handleControlPacket.assert_not_called()
            cls.return_value.writePacket.assert_not_called()
