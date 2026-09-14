"""Real compiled SP accessor and RAM capture, using synthetic MIPS32 code only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory):
    from pandare import Panda
    root = Path(directory)
    base = Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk"
    spec = importlib.util.spec_from_file_location("exception_observation", base / "exception_observation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # ori sp,zero,0x1000; break 0. SP and stack bytes have independent sentinels.
    code = bytes.fromhex("00101d340d000000")
    (root / "code.bin").write_bytes(code + bytes(0x1000-len(code)) + bytes.fromhex("78563412"))
    config = {"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin")}]}
    (root / "machine.json").write_text(json.dumps(config))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    def validate(words):
        if words != [0x1000]:
            raise ValueError("Unexpected native SP")
    capture = module.ExceptionStackCapture({"pcs": [4], "exception_index": 18, "words": 1},
        panda.libpanda.panda_current_sp_external, validate, panda.physical_memory_read)
    report = {"execution": {"completed_blocks": 0, "per_context": {}}}
    def persist():
        if report["execution"]["cpu_exceptions"]["events"] == 1:
            temporary = root / "result.tmp"
            temporary.write_text(json.dumps(report))
            temporary.replace(root / "result.json")
    module.install_exception_observer(panda, report, {}, persist, capture)
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeStackTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pandare") and os.environ.get("FIRMWIRE_TEST_NATIVE_EXCEPTION") == "1",
                         "requires explicit packaged development PANDA test opt-in")
    def test_compiled_sp_and_stack_read_complete_without_cffi_cpu_access(self):
        with tempfile.TemporaryDirectory(prefix="mtk-stack-") as directory:
            path = Path(directory) / "result.json"
            with tempfile.TemporaryFile(mode="w+") as log:
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
            event = json.loads(path.read_text())["execution"]["cpu_exceptions"]["first"][0]
            self.assertEqual(event["exception_index"], 18)
            self.assertEqual(event["stack"]["sp"], 0x1000)
            self.assertEqual(event["stack"]["words"], [0x12345678])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        unittest.main()
