"""Synthetic packaged-engine test: BREAK PC and exception-vector selection.

No firmware, private identity, registers or external network are involved.
Requires the isolated cockpit-mtk-legacy development engine.
"""
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
    spec = importlib.util.spec_from_file_location("exception_observation", Path(__file__).resolve().parents[1] /
        "firmwire/vendor/mtk/exception_observation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path(directory)
    (root / "break.bin").write_bytes(bytes.fromhex("0d000000"))  # MIPS32 BREAK 0
    (root / "vectors.bin").write_bytes(bytes(0x380) + bytes.fromhex("ffff001000000000"))
    config = {"entry_address": 0, "memory_mapping": [
        {"name": "code", "address": 0, "size": 0x200000, "file": str(root / "break.bin")},
        {"name": "exception_vectors", "address": 0xbfc00000, "size": 0x1000, "file": str(root / "vectors.bin")},
        {"name": "architectural_vectors", "address": 0x1fc00000, "size": 0x1000, "file": str(root / "vectors.bin")},
        {"name": "ebase_vectors", "address": 0x80000000, "size": 0x1000, "file": str(root / "vectors.bin")}]}
    (root / "machine.json").write_text(json.dumps(config))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    report = {"execution": {"completed_blocks": 0, "per_context": {}}}
    def persist():
        if report["execution"]["cpu_exceptions"]["events"] == 2:
            temporary = root / "result.tmp"
            temporary.write_text(json.dumps(report))
            temporary.replace(root / "result.json")
    module.install_exception_observer(panda, report, {}, persist)

    # The pinned legacy Python wrapper calls the public name but defines the
    # underscored helper. Test-local alias only; no guest/engine changes.
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeExceptionTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pandare") and os.environ.get("FIRMWIRE_TEST_NATIVE_EXCEPTION") == "1",
                         "requires explicit packaged development PANDA test opt-in")
    def test_break_is_observed_and_exception_vector_is_selected(self):
        with tempfile.TemporaryDirectory(prefix="mtk-exception-") as directory:
            result_path = Path(directory) / "result.json"
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", directory],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 10
                while not result_path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=2)
            self.assertTrue(result_path.exists(), output[-3000:])
            report = json.loads(result_path.read_text())
            events = report["execution"]["cpu_exceptions"]
            self.assertEqual(events["events"], 2)
            self.assertEqual(events["first"][0]["exception_index"], 18)
            self.assertEqual(events["first"][0]["pc"], 0)
            # This minimal legacy fixture faults fetching the selected vector;
            # it proves observation/dispatch, NOT execution of a guest handler.
            self.assertEqual(events["first"][1]["exception_index"], 28)
            self.assertEqual(events["first"][1]["pc"], 0xbfc00380)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        unittest.main()
