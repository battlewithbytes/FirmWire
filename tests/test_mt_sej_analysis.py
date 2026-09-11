"""Register-level known answers, persistent identity, and fail-closed tests.

CBC vectors: NIST SP 800-38A F.2.1/F.2.2. ECB: FIPS 197 Appendix C.
No proprietary firmware, key material or plaintext fixtures are used.
"""
import importlib.util
import json
import os
import pickle
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] /
        ("firmwire/vendor/mtk/hw/" + name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AnalysisKey = load("analysis_key").AnalysisKey
VirtualSEJ = load("sej_analysis").VirtualSEJ
IDENTITY = "00000000-0000-4000-8000-000000000001"


class SEJTests(unittest.TestCase):
    def setUp(self):
        self.key = AnalysisKey(bytes(range(32)), IDENTITY)
        self.device = VirtualSEJ(self.key)

    def words(self, offset, data):
        for i in range(0, len(data), 4):
            self.assertTrue(self.device.write(offset + i, 4, int.from_bytes(data[i:i+4], "little")))

    def block(self, data):
        self.words(0x10, data)
        self.assertTrue(self.device.write(8, 4, 1))
        self.assertEqual(self.device.read(8, 4), 0x8000)
        return b"".join(self.device.read(i, 4).to_bytes(4, "little") for i in range(0x50, 0x60, 4))

    def test_ecb_known_answers_all_key_lengths_and_directions(self):
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        for length, flag, ciphertext in (
            (16, 0, "69c4e0d86a7b0430d8cdb78070b4c55a"),
            (24, 0x10, "dda97ca4864cdfe06eaf70a0ec0d7191"),
            (32, 0x20, "8ea2b7ca516745bfeafc49904b496089")):
            with self.subTest(length=length):
                self.device.reset()
                self.words(0x20, bytes(range(length)))
                self.device.write(4, 4, flag | 1)
                encrypted = self.block(plain)
                self.assertEqual(encrypted.hex(), ciphertext)
                self.device.write(4, 4, flag)
                self.assertEqual(self.block(encrypted), plain)

    def test_cbc_known_answer_chaining_encrypt_decrypt_and_clear(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
        expected = bytes.fromhex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2")
        self.words(0x20, key)
        self.words(0x40, bytes(range(16)))
        self.device.write(4, 4, 3)
        output = self.block(plain[:16]) + self.block(plain[16:])
        self.assertEqual(output, expected)
        self.device.write(8, 4, 2)
        self.assertEqual(self.device.read(8, 4), 8)
        self.assertEqual(self.device.read(0x50, 4), 0)
        self.device.write(4, 4, 2)
        self.assertEqual(self.block(output[:16]) + self.block(output[16:]), plain)

    def bind(self, encrypt):
        self.device.write(8, 4, 2)
        self.device.write(8, 4, 0x40000008)
        self.assertEqual(self.device.read(8, 4), 0x80000000)
        self.device.write(0xc, 4, 0x10)
        self.device.write(4, 4, 3 if encrypt else 2)

    def test_firmware_style_bound_key_roundtrip_and_reset(self):
        self.words(0x60, bytes(range(32)))
        self.bind(True)
        encrypted = self.block(bytes(range(16))) + self.block(bytes(range(16, 32)))
        self.bind(False)
        self.assertEqual(self.block(encrypted[:16]) + self.block(encrypted[16:]), bytes(range(32)))
        self.device.reset()
        self.words(0x60, bytes(range(32)))
        self.bind(True)
        self.assertEqual(self.block(bytes(range(16))), encrypted[:16])
        self.assertEqual(self.device.read(0x20, 4), 0)  # Internal key is not exposed as AKEY.

    def test_different_virtual_root_cannot_decrypt_existing_ciphertext(self):
        self.bind(True)
        encrypted = self.block(bytes(range(16)))
        self.device = VirtualSEJ(AnalysisKey(bytes([99])*32, IDENTITY))
        self.bind(False)
        self.assertNotEqual(self.block(encrypted), bytes(range(16)))

    def test_feedback_is_deterministic_but_not_vendor_derivation(self):
        def sequence():
            self.device.reset()
            self.device.write(0xc, 4, 0x110)
            self.device.write(4, 4, 2)
            return [self.block(bytes(16)) for _ in range(3)]
        first = sequence()
        self.assertEqual(first, sequence())
        self.assertEqual(len(set(first)), 3)
        self.assertFalse(self.device.facts()["vendor_derivation_compatible"])

    def test_unsupported_operations_never_report_ready(self):
        for offset, size, value in [(8,4,0x123), (4,4,0x1003), (4,4,0x30),
                                   (0xc,4,0x20), (0x50,4,1), (0x100,4,0), (1,4,0), (8,1,1)]:
            with self.subTest(offset=offset, value=value):
                self.device.reset()
                self.assertFalse(self.device.write(offset, size, value))
                self.assertEqual(self.device.read(8, 4), 0)
                self.assertFalse(self.device.write(8, 4, 1))
                self.assertIsNotNone(self.device.facts()["last_fault"])
                self.assertTrue(self.device.write(8, 4, 2))
                self.assertIsNone(self.device.facts()["last_fault"])

    def test_facts_do_not_expose_root_otp_or_operands(self):
        self.words(0x60, bytes([0xaa])*32)
        self.bind(True)
        self.block(bytes([0xbb])*16)
        facts = json.dumps(self.device.facts())
        for secret in (bytes(range(32)).hex(), "aa"*32, "bb"*16):
            self.assertNotIn(secret, facts)

    @unittest.skipUnless(importlib.util.find_spec("avatar2"), "requires FirmWire runtime")
    def test_real_adapter_is_opt_in_and_observer_stays_redacted(self):
        from firmwire.emulator.firmwire import FirmWireEmu
        from firmwire.vendor.mtk.hw.AESPeripheral import AES_TOP0_Periph
        peripheral = AES_TOP0_Periph("sej", 0, 0x1000, firmwire_machine=Mock(spec=FirmWireEmu))
        self.assertIsNone(peripheral.analysis_facts())
        peripheral.enable_analysis(self.key)
        peripheral.enable_control_observer()
        peripheral.hw_write(4, 4, 1)
        peripheral.hw_write(8, 4, 1)
        self.assertEqual(peripheral.hw_read(8, 4), 0x8000)
        output = b"".join(peripheral.hw_read(i,4).to_bytes(4,"little") for i in range(0x50,0x60,4))
        self.assertEqual(output.hex(), "66e94bd4ef8a2c3b884cfa59ca342b2e")
        self.assertEqual(peripheral.analysis_facts()["operations"], 1)
        self.assertFalse(peripheral.control_observation()["key_and_payload_values_recorded"])


class AnalysisKeyTests(unittest.TestCase):
    def test_identity_cannot_leak_into_snapshot_pickle(self):
        with self.assertRaisesRegex(TypeError, "cannot be snapshotted"):
            pickle.dumps(AnalysisKey(bytes(32), IDENTITY))

    @unittest.skipUnless(importlib.util.find_spec("avatar2"), "requires FirmWire runtime")
    def test_machine_rejects_legacy_and_snapshots_before_key_creation(self):
        from firmwire.vendor.mtk.machine import MT6878Machine
        for mode, args in (("rehosted", SimpleNamespace()),
                           ("native", SimpleNamespace(restore_snapshot="saved")),
                           ("native", SimpleNamespace(snapshot_at=(0x90000000, "saved")))):
            loader = SimpleNamespace(capability_report={"startup_locations_ready": True},
                boot_mode=mode, loader_args={"sej_analysis_state": "never-created"})
            with patch("firmwire.vendor.mtk.hw.analysis_key.AnalysisKey.load_or_create") as create:
                self.assertFalse(MT6878Machine.initialize(Mock(), loader, args))
                create.assert_not_called()

    def test_new_device_instance_decrypts_after_identity_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            def operation(device, encrypt, payload):
                device.write(8, 4, 2)
                device.write(8, 4, 0x40000008)
                device.write(0xc, 4, 0x10)
                device.write(4, 4, 3 if encrypt else 2)
                for i in range(0,16,4):
                    device.write(0x10+i,4,int.from_bytes(payload[i:i+4],"little"))
                self.assertTrue(device.write(8,4,1))
                return b"".join(device.read(i,4).to_bytes(4,"little") for i in range(0x50,0x60,4))
            encrypted = operation(VirtualSEJ(AnalysisKey.load_or_create(path)), True, bytes(range(16)))
            self.assertEqual(operation(VirtualSEJ(AnalysisKey.load_or_create(path)), False, encrypted), bytes(range(16)))

    def test_persistent_private_identity_and_context_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            first = AnalysisKey.load_or_create(path)
            original = path.read_bytes()
            second = AnalysisKey.load_or_create(path)
            self.assertEqual(first.facts(), second.facts())
            self.assertEqual(first.derive(bytes(32),16), second.derive(bytes(32),16))
            self.assertNotEqual(first.derive(bytes(32),16), first.derive(bytes([1])*32,16))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_invalid_existing_identity_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            for payload in (b"private-invalid-data", b"{}", b"x"*5000):
                path.write_bytes(payload)
                path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "refusing to replace"):
                    AnalysisKey.load_or_create(path)
                self.assertEqual(path.read_bytes(), payload)

    def test_symlinks_and_public_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            AnalysisKey.load_or_create(path)
            alias = Path(directory) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                AnalysisKey.load_or_create(alias)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                AnalysisKey.load_or_create(path)
