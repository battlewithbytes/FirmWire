"""DRDI preload adapter — bridge mtkloader's boot-image load plan into FirmWire.

FirmWire parses md1img sections but not the image's CHECK_HEADER, so the
``md1drdi`` (dynamic radio data) section's real load location is unknown to it
(its MdImg ``maddr`` is 0). The external ``mtkloader`` package parses the
CHECK_HEADER and supplies that location. This adapter copies the ORIGINAL DRDI
bytes to their load location before execution begins, so the firmware can run
its own DRDI copy/relocation and initialize its radio tables.

We write only the LOAD destination (the initial payload location) and let the
firmware perform the load→runtime relocation itself. We never populate the
runtime tables ourselves.

Design notes, verified against a real MT6768 (Lagos) image:
* MTK modem physical memory is 0-based: ROM at phys 0x0 (aliased to virt
  0x90000000), RAM at phys 0x02000000 (aliased 0x92000000). CHECK_HEADER offsets
  are relative to this physical base, so the physical write address equals the
  relative ``load_offset``.
* The DRDI load region legitimately overlaps the ROM image tail (0x2e4 bytes on
  Lagos); overwriting those ROM bytes with the DRDI payload is expected.

Called from :meth:`MT6878Machine.initialize` after the ROM image is mapped and
before boot. Safe to leave in place: it is a no-op (with a warning) when
``mtkloader`` is not installed, when the image has no CHECK_HEADER, or when there
is no DRDI record.
"""

import logging

log = logging.getLogger(__name__)

# MTK modem physical base for CHECK_HEADER relative offsets (see module docstring).
PHYS_BASE = 0x0

# Default section that carries the dynamic radio data payload.
DRDI_SECTION = "md1drdi"


def preload_drdi(machine, loader, *, section_name: str = DRDI_SECTION,
                 phys_base: int = PHYS_BASE) -> bool:
    """Write the original DRDI payload to its CHECK_HEADER load location.

    Parameters
    ----------
    machine : the FirmWire machine (needs ``machine.qemu.pypanda``).
    loader  : the MTK loader (needs ``loader.md1img`` or ``loader.path``).

    Returns True if a DRDI region was preloaded, False otherwise. Never raises
    into the caller — a failure downgrades to a warning and a no-op so it cannot
    break an otherwise-working boot.
    """
    try:
        from mtkloader.boot_plan import build_boot_plan
        from mtkloader.formats.check_header import CheckHeaderError
    except ImportError:
        log.warning(
            "DRDI preload skipped: mtkloader not installed "
            "(pip install -e <mtkloader repo> to enable radio-table init)")
        return False

    img_path = getattr(loader, "md1img", None) or getattr(loader, "path", None)
    if not img_path:
        log.warning("DRDI preload skipped: loader has no md1img/path attribute")
        return False

    try:
        with open(img_path, "rb") as fh:
            data = fh.read()
        plan = build_boot_plan(data)
    except CheckHeaderError as e:
        log.warning("DRDI preload skipped: no usable CHECK_HEADER (%s)", e)
        return False
    except OSError as e:
        log.warning("DRDI preload skipped: cannot read %s (%s)", img_path, e)
        return False

    placement = next((p for p in plan.placements if p.name == section_name), None)
    if placement is None:
        log.info("DRDI preload skipped: no %s record in CHECK_HEADER", section_name)
        return False
    if placement.payload_file_offset is None or placement.payload_size is None:
        log.warning("DRDI preload skipped: %s payload location not resolved",
                    section_name)
        return False

    start = placement.payload_file_offset
    payload = data[start:start + placement.payload_size]
    if len(payload) != placement.payload_size:
        log.warning("DRDI preload skipped: payload truncated (%d of %d bytes)",
                    len(payload), placement.payload_size)
        return False

    phys = phys_base + placement.load_offset
    machine.qemu.pypanda.physical_memory_write(phys, payload)
    log.info(
        "DRDI preloaded: %#x bytes at phys %#x (load_offset=%#x, "
        "runtime_offset=%#x) — platform %s",
        len(payload), phys, placement.load_offset, placement.runtime_offset,
        plan.platform.describe())
    return True
