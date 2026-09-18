import json
import logging
import pickle
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from firmwire.hw.soc import SOCPeripheral
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.hw.ccci_ports import ClosedAPPorts
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph, Ringbuf, CCIF_PKG_HEADER, CCIF_PKG_FOOTER


def profile(channel=57):
    return {"schema": "firmwire.ccci-closed-ports/v1", "profile": "test", "source": "synthetic",
            "channels": [{"channel": channel, "name": "test-port", "max_packet_bytes": 128}]}


def packet(channel=57, body=b"private-data"):
    return struct.pack("<IIHHI", 0, 16+len(body), channel, 0, 0) + body


def ring_fixture(packets, start=0, capacity=256):
    mem = bytearray(capacity+64)
    cursor = start
    for raw in packets:
        frame = struct.pack("<II", CCIF_PKG_HEADER, len(raw)) + raw
        frame += bytes((-len(raw)) % 8)
        frame += struct.pack("<II", CCIF_PKG_FOOTER, CCIF_PKG_FOOTER)
        for byte in frame:
            mem[32+cursor] = byte
            cursor = (cursor+1) % capacity
    struct.pack_into("<III", mem, 8, start, cursor, capacity)
    return SimpleNamespace(mem=mem, offsets=[8],
        read_raw=lambda offset, size: struct.unpack_from("<I", mem, offset)[0],
        write_raw=lambda offset, size, value: struct.pack_into("<I", mem, offset, value)), cursor


class ClosedPortsTests(unittest.TestCase):
    def test_explicit_channels_bounds_privacy_and_copied_profile(self):
        source = profile()
        ports = ClosedAPPorts(source)
        source["channels"][0]["channel"] = 99
        self.assertTrue(ports.accepts(57))
        self.assertFalse(ports.accepts(99))
        for channel in (1, 99, 0xffff):
            with self.assertRaises(NotImplementedError): ports.receive(packet(channel))
        for size in range(16):
            with self.assertRaises(ValueError): ports.receive(bytes(size))
        with self.assertRaises(ValueError): ports.receive(packet(body=bytes(113)))
        first = ports.receive(packet())
        self.assertEqual(first["dropped"], 1)
        self.assertFalse(first["response_sent"])
        self.assertNotIn("private-data", json.dumps(first))
        self.assertFalse(ports.facts()["peer_connected"])
        self.assertEqual(pickle.loads(pickle.dumps(ports)).receive(packet())["dropped"], 2)
        ports.dropped[57] = (1 << 64)-1
        self.assertEqual(ports.receive(packet())["dropped"], (1 << 64)-1)
        other = ClosedAPPorts(profile(99))
        self.assertEqual(other.receive(packet(99))["dropped"], 1)

    def test_rejects_bad_profiles_and_service_collisions(self):
        invalid = [None, {}, dict(profile(), schema="unknown"), dict(profile(), channels=[])]
        for channel in (0, 0xe, 0x20, 0x22, True, -1, 65536):
            invalid.append(profile(channel))
        for limit in (True, 15, 65537):
            item = profile()
            item["channels"][0]["max_packet_bytes"] = limit
            invalid.append(item)
        invalid.extend([dict(profile(), source=""), dict(profile(), profile="")])
        duplicate = profile()
        duplicate["channels"] *= 2
        invalid.append(duplicate)
        for item in invalid:
            with self.assertRaises(ValueError): ClosedAPPorts(item)

    def test_coalesced_ring_wrap_drain_and_empty_doorbell(self):
        for start in (0, 232, 248):
            for channels in ((57, 57), (99, 57), (57, 99)):
                parent, final = ring_fixture([packet(c) for c in channels], start)
                device = object.__new__(PCCIF_Periph)
                device.pccifid, device.ringbuffer, device.rchnum = 0, parent, 0
                config = profile()
                config["channels"].append(profile(99)["channels"][0])
                device.closed_ports = ClosedAPPorts(config)
                device.log = logging.getLogger("closed-port-ring-test")
                original = bytes(parent.mem)
                with self.assertLogs(device.log, level="INFO") as logs:
                    self.assertTrue(device.hw_write(0xc, 4, 0))
                self.assertEqual(len(logs.output), 2)
                self.assertNotIn("private-data", str(logs.output))
                expected = bytearray(original)
                struct.pack_into("<I", expected, 8, final)
                self.assertEqual(parent.mem, expected)
                self.assertEqual(device.rchnum, 0)
                self.assertTrue(device.hw_write(0xc, 4, 0))
                self.assertEqual(sum(device.closed_ports.dropped.values()), 2)

    def test_ring_malformed_bounds_frames_and_partial_packet_do_not_advance(self):
        for offset, value in ((8, 3), (12, 256), (12, 3), (16, 0), (16, 1024),
                              (32, 0), (36, 0xffffffff), (12, 8), (12, 16), (72, 0), (76, 0)):
            parent, _ = ring_fixture([packet()])
            parent.write_raw(offset, 4, value)
            original_read = parent.read_raw(8, 4)
            with self.assertRaises(ValueError): Ringbuf(parent, 8).readPacket()
            self.assertEqual(parent.read_raw(8, 4), original_read)

    def test_every_aligned_ring_start_with_varied_packet_lengths(self):
        for start in range(0, 256, 8):
            for body_size in (0, 1, 4, 15, 17, 64):
                raws = [packet(body=bytes(body_size)), packet(body=b"second")]
                parent, final = ring_fixture(raws, start)
                ring = Ringbuf(parent, 8)
                for raw in raws:
                    self.assertEqual(ring.readPacket(), raw)
                self.assertIsNone(ring.readPacket())
                self.assertEqual(parent.read_raw(8, 4), final)

    def test_coalesced_existing_handlers_and_unknown_route_remain_strict(self):
        parent, _ = ring_fixture([packet(0x20), packet(0xe), packet(0), packet(99)])
        device = object.__new__(PCCIF_Periph)
        device.pccifid, device.ringbuffer = 0, parent
        device.log = logging.getLogger("coalesced-services-test")
        device.handleRPCPacket, device.handleFSPacket, device.handleControlPacket = Mock(), Mock(), Mock()
        with self.assertLogs(device.log, level="ERROR"), self.assertRaises(NotImplementedError):
            device.hw_write(0xc, 4, 0)
        for handler in (device.handleRPCPacket, device.handleFSPacket, device.handleControlPacket):
            handler.assert_called_once()

    def test_continuously_refilled_ring_has_a_finite_work_budget(self):
        parent, _ = ring_fixture([packet(0)])
        device = object.__new__(PCCIF_Periph)
        device.pccifid, device.ringbuffer = 0, parent
        device.log, device.handleControlPacket = Mock(), Mock()
        with patch("firmwire.vendor.mtk.hw.PCCIFPeripheral.Ringbuf") as cls:
            cls.return_value.offset = 8
            cls.return_value.readPacket.return_value = packet(0)
            with self.assertRaisesRegex(RuntimeError, "drain budget"):
                device.hw_write(0xc, 4, 0)
        self.assertEqual(device.handleControlPacket.call_count, 17)

    def test_loader_profile_validation_precedes_mapping_and_is_native_only(self):
        peripheral = SOCPeripheral(PCCIF_Periph, 0x123000, 0x1000, pccifid=0)
        for mode, valid in (("native", True), ("rehosted", False)):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"ccci_closed_ports": "fixture.json"}, mode
            loader.capability_report = {}
            loader.modem_soc = SimpleNamespace(peripherals=[peripheral])
            loader.build_peripheral_maps, loader.create_peripheral = Mock(), Mock()
            loader.write_capability_report = Mock()
            with patch("builtins.open", mock_open(read_data=json.dumps(profile()))):
                if valid:
                    loader.build_memory_map()
                    self.assertIsInstance(loader.create_peripheral.call_args.kwargs["closed_ports"], ClosedAPPorts)
                    self.assertNotIn("ccci_ipc", loader.capability_report)
                else:
                    with self.assertRaises(ValueError): loader.build_memory_map()
                    loader.build_peripheral_maps.assert_not_called()
            self.assertNotIn("closed_ports", peripheral._attr)
