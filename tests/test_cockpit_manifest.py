"""Stdlib checks; invoke directly, without importing FirmWire/PANDA."""
import importlib.util
import json
import mmap
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("adapter", Path(__file__).resolve().parents[1] / "firmwire/hw/cockpit_manifest.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class AdapterTests(unittest.TestCase):
    def test_shared_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backing = root / "shared.bin"
            backing.write_bytes(bytes(4096))
            manifest = {"schema": adapter.SCHEMA, "bridge": {"state": "shared-memory-only", "shared_memory": [
                {"id": "test", "ap_base": 0x70000000, "modem_base": 0x69100000,
                 "size": 4096, "path": "shared.bin", "file_offset": 0, "evidence": "test fixture"}]}}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            entries = adapter.load_manifest("manifest.json", directory)
            self.assertEqual(entries[0]["modem_base"], 0x69100000)
            window = adapter.SharedWindow(str(backing), 4096)
            try:
                with backing.open("r+b") as fh, mmap.mmap(fh.fileno(), 4096) as peer:
                    window.write(0, 4, 0xaabbccdd)
                    self.assertEqual(peer[:4], bytes.fromhex("ddccbbaa"))
                    peer[4:8] = bytes.fromhex("78563412")
                    self.assertEqual(window.read(4, 4), 0x12345678)
                with self.assertRaises(ValueError):
                    window.read(4095, 4)
            finally:
                window.close()
            manifest["bridge"]["shared_memory"][0]["path"] = "../escape"
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                adapter.load_manifest("manifest.json", directory)


if __name__ == "__main__":
    unittest.main()
