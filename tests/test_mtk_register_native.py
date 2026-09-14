"""Compiled v0 accessor and instruction callbacks; synthetic code, no firmware."""
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


def child(directory, mode):
    from pandare import Panda
    spec = importlib.util.spec_from_file_location("register_observation",
        Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/register_observation.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    root = Path(directory)
    if mode == "mips32-pause":
        # PAUSE (sll zero,zero,5) must not modify the sentinel.
        code = struct.pack("<3I", 0x34020012, 0x00000140, 0xd)
        points = {"final": 8}
    elif mode == "mips32-counter":
        code = struct.pack("<10I", 0x34020000, 0x40822002, 0x40022002, 0x24420001,
                           0x40822002, 0x40022002, 0x2442ffff, 0x40822002, 0x40022002, 0xd)
        points = {"incremented": 24, "final": 36}
    elif mode == "mips32":
        # ori v0,zero,0x12; mtc0 v0,$4,2; clear v0; mfc0 v0,$4,2; break.
        code = struct.pack("<5I", 0x34020012, 0x40822002, 0x34020000, 0x40022002, 0x0000000d)
        points = {"final": 16}
    else:
        # MIPS32 trampoline enters compressed mode at 0x100.
        code = struct.pack("<4I", 0x34040180, 0x34080101, 0x01000008, 0) + bytes(0x100 - 16)
        if mode == "mips16-pause-ll":
            # LL; PAUSE; SC; LW. PAUSE must preserve the reservation and value.
            code += bytes.fromhex("04f0c09240f1183004f0c0d2409c05e8")
            code += bytes(0x180 - len(code)) + struct.pack("<I", 0x12)
            points = {"loaded": 0x108, "stored": 0x10c, "final": 0x10e}
        elif mode == "mips16-pause":
            # li v0,0x12; PAUSE (MIPS16e2 exact encoding); break.
            code += bytes.fromhex("126a40f1183005e8")
            points = {"final": 0x106}
        elif mode == "mips16-counter":
            code += bytes.fromhex("006a41f0446740f04467014a41f0446740f04467ff4a41f0446740f0446705e8")
            points = {"incremented": 0x114, "final": 0x11e}
        else:
            code += bytes.fromhex("126a41f04467006a40f0446705e8")
            points = {"final": 0x10c}
    (root / "code.bin").write_bytes(code)
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin")}] }))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    report = {"execution": {"completed_blocks": 0}}
    def persist():
        if report["execution"]["v0_trace"]["events"][-1]["name"] != "final":
            return
        (root / "result.tmp").write_text(json.dumps(report))
        (root / "result.tmp").replace(root / "result.json")
    observer.install_v0_trace(panda, points, report, {}, persist)
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeRegisterTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_REGISTER") == "1",
                         "requires explicit packaged PANDA test opt-in")
    def test_both_modes_and_compiled_accessor(self):
        for mode in ("mips32", "mips16", "mips32-counter", "mips16-counter",
                     "mips32-pause", "mips16-pause", "mips16-pause-ll"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="mtk-v0-") as directory:
                path = Path(directory) / "result.json"
                with tempfile.TemporaryFile(mode="w+") as log:
                    proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                             "--child", directory, mode], stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 10
                        while not path.exists() and proc.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                    finally:
                        proc.terminate()
                        try: proc.wait(timeout=3)
                        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                    log.seek(0)
                    self.assertTrue(path.exists(), log.read()[-4000:])
                events = json.loads(path.read_text())["execution"]["v0_trace"]["events"]
                expected = [0x12, 1, 0x12] if mode == "mips16-pause-ll" else (
                    [1, 0] if "counter" in mode else [0x12])
                self.assertEqual([e["v0"] for e in events], expected)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child": child(sys.argv[2], sys.argv[3])
    else: unittest.main()
