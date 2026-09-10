"""Optional cockpit.devices/v1 shared-memory adapter. No invented CCIF layout.

Stdlib validation and SharedWindow are independently testable without PANDA.
The manifest is a trusted lab configuration; addresses require target evidence.
"""
import json
import mmap
import os
from pathlib import Path
import re
import stat

SCHEMA = "cockpit.devices/v1"


def load_manifest(path, work_dir=None):
    root = Path(work_dir or os.environ.get("FIRMWIRE_WORK_DIR", "/work")).resolve()

    def confined(name):
        p = (root / name).resolve()
        if root not in p.parents:
            raise ValueError("manifest/backing path escapes work directory")
        return p

    source = confined(path)
    with source.open("rb") as fh:
        data = fh.read((1 << 20) + 1)
    if len(data) > 1 << 20:
        raise ValueError("manifest too large")
    doc = json.loads(data)
    bridge = doc.get("bridge", {})
    if doc.get("schema") != SCHEMA or bridge.get("state") != "shared-memory-only":
        raise ValueError("expected cockpit.devices/v1 shared-memory-only manifest")
    entries = bridge.get("shared_memory")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ValueError("expected 1..16 shared mappings")
    result, names, files = [], set(), set()
    for e in entries:
        name = e.get("id")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name) or name in names:
            raise ValueError("invalid/duplicate mapping id")
        a, ap, size = (e.get(k) for k in ("modem_base", "ap_base", "size"))
        if any(type(v) is not int or v < 0 or v % 4096 for v in (a, ap, size)):
            raise ValueError("unaligned/invalid mapping")
        if not 4096 <= size <= 64 << 20 or a + size > 1 << 32 or ap + size > 1 << 64:
            raise ValueError("mapping exceeds supported bounds")
        if e.get("file_offset") != 0 or not e.get("evidence"):
            raise ValueError("v1 requires zero file offset and translation evidence")
        p = confined(e["path"])
        info = p.stat()
        if p in files or not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError("backing file must be a unique regular file of exactly size bytes")
        if any(a < r["modem_base"] + r["size"] and r["modem_base"] < a + size for r in result):
            raise ValueError("overlapping modem mappings")
        result.append(dict(e, path=str(p)))
        names.add(name)
        files.add(p)
    return result


class SharedWindow:
    def __init__(self, path, size):
        self.file = open(path, "r+b")
        try:
            if os.fstat(self.file.fileno()).st_size != size:
                raise ValueError("shared file changed size")
            self.mapping = mmap.mmap(self.file.fileno(), size)
        except Exception:
            self.file.close()
            raise
        self.size = size

    def _check(self, offset, size):
        if size not in (1, 2, 4, 8) or offset < 0 or offset + size > self.size:
            raise ValueError("shared access out of bounds")

    def read(self, offset, size):
        self._check(offset, size)
        return int.from_bytes(self.mapping[offset:offset+size], "little")

    def write(self, offset, size, value):
        self._check(offset, size)
        self.mapping[offset:offset+size] = value.to_bytes(size, "little")

    def close(self):
        self.mapping.close()
        self.file.close()


def peripheral_type():
    from .peripheral import FirmWirePeripheral

    class ManifestSharedMemory(FirmWirePeripheral):
        def __init__(self, name, address, size, **kwargs):
            super().__init__(name, address, size, **kwargs)
            self.window = SharedWindow(kwargs["backing_path"], size)

        def hw_read(self, offset, size):
            return self.window.read(offset, size)

        def hw_write(self, offset, size, value):
            self.window.write(offset, size, value)
            return True

        def pre_snapshot_handler(self, snapshot_name):
            raise RuntimeError("live shared-memory snapshots require coordinated AP/modem pause and capture")

    return ManifestSharedMemory


def attach_manifest(machine, path, work_dir=None):
    entries = load_manifest(path, work_dir)
    kind = peripheral_type()
    # create_peripheral overwrites ranges. Restrict replacement to an exact
    # existing RAM window; never remove a peripheral or a portion of a region.
    for e in entries:
        existing = machine.avatar.get_memory_range(e["modem_base"])
        if (existing is None or existing.address != e["modem_base"] or
                existing.size != e["size"] or getattr(existing, "forwarded", False)):
            raise ValueError("shared modem mapping must replace one exact existing RAM window")
    return [machine.create_peripheral(kind, e["modem_base"], e["size"],
        name="COCKPIT_SHMEM_" + e["id"], backing_path=e["path"]) for e in entries]
