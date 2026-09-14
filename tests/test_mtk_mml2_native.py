"""Synthetic guest MMIO through the real adapter at two non-board addresses."""
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


def child(directory):
    from pandare import Panda
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.MML2MMUPeripheral import MML2MMU93Peripheral
    root = Path(directory)
    # Bank 1 invalidate + read; bank 2 read(reset), invalidate twice + read.
    # Store guest-observed responses independently at RAM 0x1000..0x1008.
    instructions = [0x3C080040, 0x3C098000, 0x3529C000, 0xAD090040,
                    0x8D020040, 0xAC021000, 0x3C080060, 0x8D030040,
                    0xAC031004, 0xAD090040, 0xAD090040, 0x8D020040,
                    0xAC021008, 0x0000000D]
    (root / "code.bin").write_bytes(struct.pack("<%dI" % len(instructions), *instructions))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin")}] }))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    devices = {base: MML2MMU93Peripheral("bank-%x" % base, base, 0x1000,
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000, 0x600000)}
    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        base = address & ~0xFFF
        if base not in devices:
            return False
        return devices[base].hw_write(address - base, size, value)
    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, value):
        base = address & ~0xFFF
        if base not in devices:
            return False
        value[0] = devices[base].hw_read(address - base, size)
        return True
    @panda.cb_before_handle_exception
    def exception(cpu, index):
        output = root / "result.json"
        if not output.exists():
            report = {"exception_index": index, "pc": int(panda.libpanda.panda_current_pc(cpu)),
                      "guest_words": list(struct.unpack("<3I", panda.physical_memory_read(0x1000, 12))),
                      "devices": [device.control_facts() for device in devices.values()]}
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(output)
        return index
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeMML2Tests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pandare") and os.environ.get("FIRMWIRE_TEST_NATIVE_EXCEPTION") == "1",
                         "requires explicit packaged development PANDA test opt-in")
    def test_guest_mmio_completion_and_relocated_instance_isolation(self):
        with tempfile.TemporaryDirectory(prefix="mtk-mml2-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            path = Path(directory) / "result.json"
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", directory],
                stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            log.seek(0)
            self.assertTrue(path.exists(), log.read()[-3000:])
            report = json.loads(path.read_text())
            self.assertEqual((report["exception_index"], report["pc"]), (18, 13 * 4))
            self.assertEqual(report["guest_words"], [0xC000, 0, 0xC000])
            self.assertEqual([d["invalidate_all_completed"] for d in report["devices"]], [1, 2])
            self.assertTrue(all(not d["translation_supported"] for d in report["devices"]))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        unittest.main()
