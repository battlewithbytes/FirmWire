import json
import logging
import pickle
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from firmwire.hw.soc import SOCPeripheral
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph, CCIF_PKG_HEADER, CCIF_PKG_FOOTER
from firmwire.vendor.mtk.hw.ccci_ipc import (
    decode_ilm32, IPCEndpoint, IPCDispatcher, unavailable_wmt_dispatcher,
)


def packet(ap=0x80000003, destination=0x404, message=0x8000005f, body=b"private-data"):
    local = struct.pack("<BBH", 1, 0, 4 + len(body)) + body
    return (struct.pack("<IIHHI", 0, 40 + len(local), 0x22, 0, ap)
            + struct.pack("<6I", 13, destination, 0, message, 0xdeadbeef, 0) + local)


class RecordingEndpoint(IPCEndpoint):
    def __init__(self):
        self.messages = []

    def receive(self, message):
        self.messages.append(message)
        return {"disposition": "test-only"}


class IPCServiceTests(unittest.TestCase):
    def test_wmt_unavailable_accepts_notifications_not_one_image_message(self):
        dispatcher = unavailable_wmt_dispatcher()
        for index, message in enumerate((0, 17, 0x8000005f, 0xffffffff), 1):
            disposition = dispatcher.receive(packet(message=message))
            self.assertEqual(disposition["dropped"], index)
            self.assertEqual(disposition["disposition"], "dropped-peer-unavailable")
            for key in ("peer_connected", "application_verified", "response_sent"):
                self.assertFalse(disposition[key])
            self.assertNotIn("private-data", json.dumps(disposition))
        self.assertEqual(pickle.loads(pickle.dumps(dispatcher)).receive(packet())["dropped"], 5)
        self.assertEqual(unavailable_wmt_dispatcher().receive(packet())["dropped"], 1)

    def test_reusable_routes_are_independent_and_unknowns_fail(self):
        first, second = RecordingEndpoint(), RecordingEndpoint()
        routes = {(123, 456): first, (789, 123): second}
        dispatcher = IPCDispatcher(routes)
        routes.clear()
        dispatcher.receive(packet(ap=123, destination=456))
        self.assertEqual(len(first.messages), 1)
        self.assertEqual(second.messages, [])
        self.assertNotIn("private-data", repr(first.messages[0]))
        for ap, destination in ((123, 123), (789, 456), (0x80000003, 0x404)):
            with self.assertRaises(NotImplementedError):
                dispatcher.receive(packet(ap=ap, destination=destination))
        self.assertEqual(len(first.messages), 1)

    def test_malformed_frames_never_reach_endpoint(self):
        endpoint = RecordingEndpoint()
        dispatcher = IPCDispatcher({(0x80000003, 0x404): endpoint})
        raw = packet()
        for size in range(len(raw)):
            with self.assertRaises(ValueError): dispatcher.receive(raw[:size])
        for offset, value in ((0, 1), (4, len(raw)+1), (8, 0x20), (36, 1)):
            bad = bytearray(raw)
            struct.pack_into("<I", bad, offset, value)
            with self.assertRaises((ValueError, NotImplementedError)): dispatcher.receive(bad)
        bad = bytearray(raw)
        struct.pack_into("<H", bad, 42, 4)
        with self.assertRaises(ValueError): dispatcher.receive(bad)
        with self.assertRaises(ValueError): dispatcher.receive(packet(body=b"x"*1021))
        self.assertEqual(endpoint.messages, [])
        # Pointer values are wire artifacts, not host/guest addresses to read.
        for value in (0, 1, 0xffffffff):
            valid = bytearray(raw)
            struct.pack_into("<I", valid, 32, value)
            self.assertEqual(decode_ilm32(valid).local_parameter[4:], b"private-data")

    def test_configuration_validation_and_bounded_counter(self):
        for route in ((True, 1), (-1, 2), (1,), "bad"):
            with self.assertRaises(ValueError): IPCDispatcher({route: RecordingEndpoint()})
        with self.assertRaises(ValueError): IPCDispatcher({(1, 2): object()})
        for limit in (True, -1, 3, 1.5):
            with self.assertRaises(ValueError): IPCDispatcher({}, max_local_parameter=limit)
        dispatcher = unavailable_wmt_dispatcher()
        endpoint = dispatcher.routes[(0x80000003, 0x404)]
        endpoint.dropped = (1 << 64) - 1
        self.assertEqual(dispatcher.receive(packet())["dropped"], (1 << 64) - 1)

    def test_real_ring_sink_never_writes_response_or_interrupt(self):
        raw = packet()
        padded = (len(raw)+7) & ~7
        occupied = padded+16
        mem = bytearray(512)
        struct.pack_into("<III", mem, 8, 0, occupied, 256)
        struct.pack_into("<II", mem, 32, CCIF_PKG_HEADER, len(raw))
        mem[40:40+len(raw)] = raw
        struct.pack_into("<II", mem, 40+padded, CCIF_PKG_FOOTER, CCIF_PKG_FOOTER)
        parent = SimpleNamespace(mem=mem, offsets=[8],
            read_raw=lambda offset, size: struct.unpack_from("<I", mem, offset)[0],
            write_raw=lambda offset, size, value: struct.pack_into("<I", mem, offset, value))
        device = object.__new__(PCCIF_Periph)
        device.pccifid, device.ringbuffer, device.rchnum = 0, parent, 0
        device.ipc_dispatcher = unavailable_wmt_dispatcher()
        device.log = logging.getLogger("ipc-ring-test")
        original = bytes(mem)
        with self.assertLogs(device.log, level="INFO") as logs:
            self.assertTrue(device.hw_write(0xc, 4, 0))
        expected = bytearray(original)
        struct.pack_into("<I", expected, 8, occupied)
        self.assertEqual(mem, expected)  # Only the existing RX read pointer moves.
        self.assertEqual(device.rchnum, 0)
        self.assertNotIn("private-data", str(logs.output))
        device.ipc_dispatcher = None
        parent.write_raw(8, 4, 0)
        with self.assertLogs(device.log, level="ERROR"), self.assertRaises(AssertionError):
            device.hw_write(0xc, 4, 0)

    def test_loader_policy_is_explicit_native_and_instance_local(self):
        peripheral = SOCPeripheral(PCCIF_Periph, 0x123000, 0x1000, pccifid=0)
        for policy in ("disabled", "wmt-unavailable"):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"ccci_ipc": policy}, "native"
            loader.capability_report = {}
            loader.modem_soc = SimpleNamespace(peripherals=[peripheral])
            loader.build_peripheral_maps = Mock()
            loader.create_soc_peripheral, loader.create_peripheral = Mock(), Mock()
            loader.write_capability_report = Mock()
            loader.build_memory_map()
            self.assertNotIn("ipc_dispatcher", peripheral._attr)
            if policy == "disabled":
                loader.create_soc_peripheral.assert_called_once_with(peripheral)
                loader.create_peripheral.assert_not_called()
            else:
                self.assertIsInstance(loader.create_peripheral.call_args.kwargs["ipc_dispatcher"], IPCDispatcher)
                self.assertFalse(loader.capability_report["ccci_ipc"]["peer_connected"])
        for policy, mode in (("unknown", "native"), ("wmt-unavailable", "rehosted")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"ccci_ipc": policy}, mode
            with self.assertRaises(ValueError): loader.build_memory_map()
