"""Tests for the DRDI preload adapter (firmwire/vendor/mtk/drdi_preload.py).

The adapter is loaded by file path so the test does not pull in FirmWire's
PANDA/avatar runtime. A fake machine records the guest-memory write; the payload
is checked against bytes read INDEPENDENTLY from the image (not via the same code
path that produced the plan).

Requires the external `mtkloader` package on sys.path and LAGOS_MODEM_IMG set to
a real Lagos modem image; otherwise the real-image tests skip.
"""

import importlib.util
import os
import pathlib

import pytest

# Load the adapter module directly, avoiding firmwire.vendor.mtk package imports.
_ADAPTER = pathlib.Path(__file__).resolve().parents[1] / "firmwire" / "vendor" / "mtk" / "drdi_preload.py"
_spec = importlib.util.spec_from_file_location("drdi_preload", _ADAPTER)
drdi_preload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drdi_preload)


def _mtkloader_available():
    try:
        import mtkloader  # noqa: F401
        return True
    except ImportError:
        return False


class FakePanda:
    def __init__(self):
        self.writes = []  # list of (addr, bytes)

    def physical_memory_write(self, addr, data):
        self.writes.append((addr, bytes(data)))


class FakeQemu:
    def __init__(self):
        self.pypanda = FakePanda()


class FakeMachine:
    def __init__(self):
        self.qemu = FakeQemu()


class FakeLoader:
    def __init__(self, md1img):
        self.md1img = md1img
        self.path = md1img


_IMG = os.environ.get("LAGOS_MODEM_IMG")
_LAGOS_DRDI_LOAD_OFF = 0x129E900
_LAGOS_DRDI_LOAD_SIZE = 0x11A6E0
_LAGOS_DRDI_FILE_OFF = 0x129FEE0


@pytest.mark.skipif(not (_IMG and _mtkloader_available()),
                    reason="needs LAGOS_MODEM_IMG and the mtkloader package")
def test_preloads_drdi_at_load_offset():
    machine, loader = FakeMachine(), FakeLoader(_IMG)
    ok = drdi_preload.preload_drdi(machine, loader)
    assert ok is True

    writes = machine.qemu.pypanda.writes
    assert len(writes) == 1
    addr, payload = writes[0]

    # physical base is 0, so the write address equals the relative load_offset
    assert addr == drdi_preload.PHYS_BASE + _LAGOS_DRDI_LOAD_OFF
    assert len(payload) == _LAGOS_DRDI_LOAD_SIZE

    # payload must equal the md1drdi section bytes, read independently here
    expected = open(_IMG, "rb").read()[_LAGOS_DRDI_FILE_OFF:
                                        _LAGOS_DRDI_FILE_OFF + _LAGOS_DRDI_LOAD_SIZE]
    assert payload == expected


@pytest.mark.skipif(not _mtkloader_available(),
                    reason="needs the mtkloader package")
def test_missing_image_is_noop():
    machine = FakeMachine()
    loader = FakeLoader("/nonexistent/does-not-exist.img")
    assert drdi_preload.preload_drdi(machine, loader) is False
    assert machine.qemu.pypanda.writes == []


def test_no_loader_path_is_noop():
    class Bare:
        pass
    machine = FakeMachine()
    # A loader with neither md1img nor path -> no-op (only reachable when
    # mtkloader is importable; otherwise the import guard returns first).
    result = drdi_preload.preload_drdi(machine, Bare())
    assert result is False
    assert machine.qemu.pypanda.writes == []
