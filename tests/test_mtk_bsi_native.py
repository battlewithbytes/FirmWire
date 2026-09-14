"""Firmware-free native MMIO test; opt-in, bounded child process."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory, read_completion=False):
    from pandare import Panda
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
    root = Path(directory)
    instructions = [0x3c080040, 0x8d021008, 0xac021000, 0x34091234, 0xad091004,
                    0x34090301, 0xad091000, 0x8d021008, 0xac021004, 0x8d021108,
                    0xac021008, 0x3c080060, 0x8d021008, 0xac02100c, 0xd]
    if read_completion:
        instructions = [0x3c080040, 0x34090003, 0xad091000, 0x8d021204, 0xac021000,
                        0x8d02100c, 0xac021004, 0x8d021010, 0xac021008,
                        0x34090001, 0xad091200, 0x8d021204, 0xac02100c,
                        0x3c080060, 0x8d021204, 0xac021010, 0xd]
    (root / "code.bin").write_bytes(struct.pack("<%dI" % len(instructions), *instructions))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin")}] }))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    devices = {base: BSIImmediatePeripheral("bank-%x" % base, base, 0x9000, bsi_mode="pending",
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000, 0x600000)}

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base + 0x9000:
                result = device.hw_write(address-base, size, value)
                if read_completion and address == base + 0x1000 and value == 3:
                    # Explicit test backend supplies unrelated 36-bit data. No
                    # production code, firmware identity or poll chooses it.
                    pending = device.control.pending[0]
                    device.control.complete_read(0, pending.sequence, 0xa12345678)
                return result
        return False

    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base + 0x9000:
                value[0] = device.hw_read(address-base, size)
                return True
        return False

    @panda.cb_before_handle_exception
    def exception(cpu, index):
        output = root / "result.json"
        if not output.exists():
            report = {"exception_index": index, "pc": int(panda.libpanda.panda_current_pc(cpu)),
                      "guest_words": list(struct.unpack("<5I" if read_completion else "<4I",
                          panda.physical_memory_read(0x1000, 20 if read_completion else 16))),
                      "devices": [device.control_observation() for device in devices.values()]}
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(output)
        return index
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeBsiTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1",
                         "requires explicit development PANDA opt-in")
    def test_guest_sees_ready_then_pending_and_other_banks_are_isolated(self):
        report = self.run_child()
        self.assertEqual((report["exception_index"], report["pc"]), (18, 56))
        self.assertEqual(report["guest_words"], [1, 0, 1, 1])
        command = report["devices"][0]["pending"][0]
        self.assertEqual((command["port"], command["data"][0]), (3, 0x1234))
        self.assertEqual(report["devices"][1]["pending"], [])
        self.assertFalse(report["devices"][0]["backend_connected"])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1",
                         "requires explicit development PANDA opt-in")
    def test_guest_reads_explicit_backend_payload_and_acknowledges(self):
        report = self.run_child("--child-read")
        self.assertEqual((report["exception_index"], report["pc"]), (18, 64))
        self.assertEqual(report["guest_words"], [1, 0x12345678, 0xa, 0, 0])
        self.assertEqual(report["devices"][0]["completed_reads"], 1)
        self.assertEqual(report["devices"][1]["completed_reads"], 0)

    def run_child(self, mode="--child"):
        with tempfile.TemporaryDirectory(prefix="mtk-bsi-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            path = Path(directory) / "result.json"
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), mode, directory],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
            finally:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
            log.seek(0)
            self.assertTrue(path.exists(), log.read()[-3000:])
            return json.loads(path.read_text())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--child", "--child-read"):
        child(sys.argv[2], sys.argv[1] == "--child-read")
    else: unittest.main()
