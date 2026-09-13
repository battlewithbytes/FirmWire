"""Protocol tests without vendor images; imports the real PCCIF dispatcher."""
import logging
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from firmwire.vendor.mtk.hw.PCCIFPeripheral import (
    PCCIF_Periph, CCCI_RPC_TX, IPC_RPC_QUERY_AP_SYS_PROPERTY,
    IPC_RPC_GET_GPIO_ADC_OP, IPC_RPC_GET_EINT_ATTR_OP, IPC_RPC_DTSI_QUERY_OP,
)
from firmwire.vendor.mtk.hw.ap_properties import APSystemProperties


def request(packets, op=IPC_RPC_QUERY_AP_SYS_PROPERTY):
    payload = b"".join(struct.pack("<I", len(p)) + p + b"\x00" * (-len(p) % 4)
                       for p in packets)
    return struct.pack("<IIHHIII", 0, 24 + len(payload), 32, 0x8123, 7,
                       op, len(packets)) + payload


class PropertyRPCTests(unittest.TestCase):
    def setUp(self):
        self.device = PCCIF_Periph.__new__(PCCIF_Periph)
        self.device.log = logging.getLogger("property-rpc-test")
        self.device.ap_properties = APSystemProperties()
        self.writes = []
        self.ring = SimpleNamespace(writePacket=self.writes.append)

    def exchange(self, packets, op=IPC_RPC_QUERY_AP_SYS_PROPERTY):
        self.device.handleRPCPacket(self.ring, request(packets, op))
        result = self.writes[-1]
        header = struct.unpack_from("<IIHHIII", result)
        self.assertEqual(header[:6], (0, len(result), CCCI_RPC_TX, 0x8123, 7, op | 0xffff0000))
        offset, output = 24, []
        for _ in range(header[6]):
            length = struct.unpack_from("<I", result, offset)[0]
            offset += 4
            output.append(result[offset:offset + length])
            pad = -length % 4
            self.assertEqual(result[offset + length:offset + length + pad], b"\x00" * pad)
            offset += length + pad
        self.assertEqual(offset, len(result))
        return output

    def test_absent_property_is_empty_not_invented_sku(self):
        with patch.dict(os.environ, {"ro.boot.hardware.sku": "host-must-not-leak"}):
            self.assertEqual(self.exchange([b"ro.boot.hardware.sku"]), [b"\x00" * 4, b"\x00"])

    def test_captured_lagos_request_matches_reference_framing(self):
        # Metadata-only request captured at the previous host assertion.
        captured = bytes.fromhex(
            "000000003400000020000000000000000f4000000100000015000000"
            "726f2e626f6f742e68617264776172652e736b7500000000")
        self.device.handleRPCPacket(self.ring, captured)
        response = self.writes[-1]
        self.assertEqual(struct.unpack_from("<IIHHIII", response),
                         (0, 40, CCCI_RPC_TX, 0, 0, 0xffff400f, 2))
        self.assertEqual(response[24:], bytes.fromhex("04000000000000000100000000000000"))

    def test_explicit_values_and_all_alignment_residues(self):
        for value in ("", "a", "ab", "abc", "abcd", "é", "x" * 91):
            with self.subTest(value=value):
                self.device.ap_properties = APSystemProperties({"test.key": value}, source="synthetic-unit-fixture")
                self.assertEqual(self.exchange([b"test.key\x00"]),
                                 [struct.pack("<i", len(value.encode())), value.encode() + b"\x00"])

    def test_repeated_query_does_not_retain_previous_value(self):
        self.device.ap_properties = APSystemProperties({"present": "yes"}, source="synthetic-unit-fixture")
        self.exchange([b"present"])
        self.assertEqual(self.exchange([b"absent"]), [b"\x00" * 4, b"\x00"])

    def test_invalid_names_and_counts_return_protocol_error(self):
        for packets in ([], [b""], [b"\x00"], [b"x\x00y"], [b"\xff"], [b"x", b"y"]):
            with self.subTest(packets=packets):
                self.assertEqual(self.exchange(packets), [struct.pack("<i", -2)])

    def test_truncated_framing_never_writes_a_success_reply(self):
        packet = request([b"abcd"])
        for size in (0, 15, 23, 24, 25, 27, 28, 31):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.device.handleRPCPacket(self.ring, packet[:size])
        self.assertEqual(self.writes, [])

    def test_unknown_operation_remains_visible_failure(self):
        with self.assertRaises(AssertionError):
            self.device.handleRPCPacket(self.ring, request([], 0x7777))
        self.assertEqual(self.writes, [])

    def test_existing_rpc_payloads_unchanged_with_correct_stream_length(self):
        self.assertEqual(self.exchange([], IPC_RPC_GET_GPIO_ADC_OP), [b"\x00" * 4, b"\x00" * 96])
        self.assertEqual(self.exchange([b"sim", b"", b"\x00" * 4], IPC_RPC_GET_EINT_ATTR_OP),
                         [b"\x00" * 4, b"\x00" * 4])
        self.assertEqual(self.exchange([b"\x01\x00" + b"test".ljust(64, b"\x00")], IPC_RPC_DTSI_QUERY_OP),
                         [b"\x00" * 68])

    def test_profile_is_copied_and_requires_provenance(self):
        values = {"key": "value"}
        with self.assertRaises(ValueError):
            APSystemProperties(values)
        profile = APSystemProperties(values, source="synthetic-unit-fixture")
        values["key"] = "changed"
        self.assertEqual(profile.query([b"key"])[2][1], b"value\x00")

    def test_invalid_profile_fails_before_boot(self):
        for values in ({"x": "y" * 92}, {"x": "é" * 46}, {"": "a"}, {"x": 2}, {"x": "a\x00b"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                APSystemProperties(values, source="synthetic-unit-fixture")


if __name__ == "__main__":
    unittest.main()
