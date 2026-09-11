## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
"""FirmWire (modem) side of the AP<->modem CCCI co-simulation bridge.

Two peripherals plus a wiring helper. They are **optional** -- nothing here runs
unless a machine explicitly attaches the bridge (see :func:`attach_ccci_bridge`
and :func:`maybe_attach_from_env`), so a normal FirmWire run is unaffected.

* :class:`CCCISharedMemoryPeripheral` backs the modem's CCCI SMEM window with a
  file-backed shared ``mmap`` (:class:`firmwire.hw.ccci_broker.SharedRegion`)
  instead of a private avatar range. QEMU maps the same file as guest RAM at the
  AP-physical ``ccci_region_base`` (``memory-backend-file,share=on``), so a byte
  the modem writes into SMEM is immediately visible to the AP kernel and vice
  versa. This is "Option A" from ``qemu/docs/2026-09-10-cosim-architecture.md``.

* :class:`CCIFDoorbellPeripheral` models the CCIF mailbox: a write to its notify
  register rings the peer via :class:`~firmwire.hw.ccci_broker.DoorbellClient`;
  an incoming ring from the peer sets a pending/status bit the modem polls and
  (phase c, PENDING) would raise the modem's CCIF IRQ. This is "Option A'".

Why a peripheral rather than avatar2's ``file=`` memory range: avatar2's
file-backed range loads a file's *contents* into an otherwise private range; it
does not keep the mapping live-shared MAP_SHARED with another process. Servicing
the window through a peripheral that reads/writes a MAP_SHARED ``mmap`` gives the
zero-copy cross-process visibility the bridge needs, and mirrors the existing
``PassthroughPeripheral`` (which backs its window with a private Python list).

NOTE: this module imports avatar2 (via :class:`FirmWirePeripheral`). The broker,
shared-memory, and doorbell primitives live in :mod:`firmwire.hw.ccci_broker`,
which is pure standard library and importable without avatar2 for testing.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .peripheral import FirmWirePeripheral
from .ccci_broker import (
    SharedRegion,
    DoorbellClient,
    DEFAULT_SHM_PATH,
    DEFAULT_SOCK_PATH,
    DEFAULT_REGION_SIZE,
    POLL_AREA_SIZE,
)

log = logging.getLogger(__name__)


class CCCISharedMemoryPeripheral(FirmWirePeripheral):
    """CCCI SMEM window backed by a MAP_SHARED file the AP also maps.

    Drop-in for the private RAM range the MTK machine currently uses for
    ``RAW_MDCCCI_DBG``: same ``hw_read``/``hw_write`` contract, but every access
    goes to the shared ``mmap`` so QEMU and FirmWire share the bytes.
    """

    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, **kwargs)

        self.shm_path = kwargs.get("ccci_shared_path", DEFAULT_SHM_PATH)
        # The shared file must be at least as large as the window we service.
        # A larger file is fine (the AP may map a superset); we index by offset.
        self._region_size = max(size, kwargs.get("ccci_region_size", size))
        self.region = SharedRegion(self.shm_path, self._region_size, create=True)

        self.read_handler[0:size] = self.hw_read
        self.write_handler[0:size] = self.hw_write

    def hw_read(self, offset, size):
        value = self.region.read(offset, size)
        self.log_read(value, size, "SMEM %08x" % (self.address + offset))
        return value

    def hw_write(self, offset, size, value):
        self.log_write(value, size, "SMEM %08x" % (self.address + offset))
        self.region.write(offset, size, value)
        return True

    # -- snapshot support: the mmap is a live host resource, not picklable --- #
    def __getstate__(self):
        state = super().__getstate__()
        # Drop the live mapping; it is re-opened in post_snapshot_restore.
        state.pop("region", None)
        return state

    def post_snapshot_restore_handler(self, snapshot_name):
        # Re-attach to the same shared file after a restore.
        self.region = SharedRegion(self.shm_path, self._region_size, create=True)


# CCIF register layout (small, arbitrary but stable offsets -- the real MT6765
# CCIF register map is a reverse-engineering target, see the impl doc):
#   0x00  TX / notify   (write channel bitmap -> ring peer)
#   0x04  RX / pending  (read: channels the peer has rung; write: ack/clear)
#   0x08  RX seq        (read-only: bumps on each peer ring, for edge detect)
#   0x0c  status/enable
CCIF_REG_TX = 0x00
CCIF_REG_RX = 0x04
CCIF_REG_RX_SEQ = 0x08
CCIF_REG_STATUS = 0x0C
CCIF_REG_SPAN = 0x10


class CCIFDoorbellPeripheral(FirmWirePeripheral):
    """CCIF mailbox doorbell (modem side).

    A write to :data:`CCIF_REG_TX` rings the AP via the doorbell client. An
    incoming ring from the AP OR's the channel bit into a pending register and
    bumps the RX-seq counter; the modem's CCIF handler polls those (phase b). A
    real IRQ (phase c) would be raised from :meth:`_on_peer_ring` -- see
    :meth:`raise_ccif_interrupt`, which is a documented stub until the CCIF IRQ
    number and a PANDA injection path are known.
    """

    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, **kwargs)

        self.sock_path = kwargs.get("ccci_doorbell_sock", DEFAULT_SOCK_PATH)
        # Share the poll-flag area with the SMEM region if one was passed, so
        # poll-only mode has somewhere to flip a flag the AP can watch.
        region = kwargs.get("ccci_region", None)

        self._pending = 0
        self._rx_seq = 0
        self._status = 0

        self.doorbell = DoorbellClient(self.sock_path, region=region, name="modem")
        # connect() never raises; falls back to poll-only if no broker.
        self.doorbell.connect(on_ring=self._on_peer_ring)

    # -- guest MMIO --------------------------------------------------------- #
    def hw_read(self, offset, size):
        if offset == CCIF_REG_TX:
            value = 0
        elif offset == CCIF_REG_RX:
            value = self._pending
        elif offset == CCIF_REG_RX_SEQ:
            value = self._rx_seq
        elif offset == CCIF_REG_STATUS:
            value = self._status
        else:
            value = 0
        self.log_read(value, size, "CCIF+%x" % offset)
        return value

    def hw_write(self, offset, size, value):
        self.log_write(value, size, "CCIF+%x" % offset)
        if offset == CCIF_REG_TX:
            # Ring the AP for each channel bit set (channel == bit index).
            if value == 0:
                self.doorbell.ring(0, 1)
            else:
                for ch in range(32):
                    if value & (1 << ch):
                        self.doorbell.ring(ch, 1)
        elif offset == CCIF_REG_RX:
            # Write-to-clear the acknowledged pending bits.
            self._pending &= ~value & 0xFFFFFFFF
        elif offset == CCIF_REG_STATUS:
            self._status = value
        return True

    # -- peer -> us --------------------------------------------------------- #
    def _on_peer_ring(self, channel: int, value: int) -> None:
        """Called on the doorbell reader thread when the AP rings us."""
        self._pending |= (1 << (channel & 31))
        self._rx_seq = (self._rx_seq + 1) & 0xFFFFFFFF
        self.log.debug("CCIF RX ring ch=%d (pending=%#x seq=%d)",
                       channel, self._pending, self._rx_seq)
        self.raise_ccif_interrupt(channel)

    def raise_ccif_interrupt(self, channel: int) -> None:
        """Inject the modem's CCIF IRQ (phase c -- PENDING).

        Poll-first (phase b) needs nothing here: the modem reads
        :data:`CCIF_REG_RX` / :data:`CCIF_REG_RX_SEQ`. To raise a real IRQ the
        implementation needs the MT6765 CCIF IRQ line and a PANDA/avatar
        injection call (e.g. ``self.machine.qemu.pypanda`` interrupt API); both
        are reverse-engineering / FirmWire-arm64 gated, so this is intentionally
        a no-op stub. See qemu/docs/2026-09-10-ccci-bridge-impl.md.
        """
        return

    def __getstate__(self):
        state = super().__getstate__()
        # DoorbellClient owns a socket + thread -- not picklable.
        state.pop("doorbell", None)
        return state

    def post_snapshot_restore_handler(self, snapshot_name):
        self.doorbell = DoorbellClient(self.sock_path, name="modem")
        self.doorbell.connect(on_ring=self._on_peer_ring)


# --------------------------------------------------------------------------- #
# Wiring helpers (the enable path)
# --------------------------------------------------------------------------- #
def attach_ccci_bridge(machine, *, smem_base: int, smem_size: int,
                       ccif_base: int, ccif_size: int = CCIF_REG_SPAN,
                       shm_path: str = DEFAULT_SHM_PATH,
                       sock_path: str = DEFAULT_SOCK_PATH,
                       region_size: Optional[int] = None):
    """Attach the shared-memory + doorbell peripherals to ``machine``.

    Call this *after* the machine's normal memory map is applied (it uses
    ``create_peripheral`` with ``overwrite=True``, so it can replace an existing
    private range such as the MTK ``RAW_MDCCCI_DBG`` window). Returns the two
    peripheral objects ``(smem_periph, ccif_periph)``.

    Nothing here is called on a default FirmWire run; a machine opts in.
    """
    if region_size is None:
        region_size = max(smem_size, DEFAULT_REGION_SIZE)

    smem = machine.create_peripheral(
        CCCISharedMemoryPeripheral, smem_base, smem_size,
        name="CCCI_SMEM_SHARED",
        ccci_shared_path=shm_path,
        ccci_region_size=region_size,
    )
    ccif = machine.create_peripheral(
        CCIFDoorbellPeripheral, ccif_base, ccif_size,
        name="CCIF_DOORBELL",
        ccci_doorbell_sock=sock_path,
        ccci_region=getattr(smem, "region", None),
    )
    log.info("CCCI bridge attached: SMEM @ %#x (%#x), CCIF @ %#x, shm=%s",
             smem_base, smem_size, ccif_base, shm_path)
    return smem, ccif


def maybe_attach_from_env(machine, *, smem_base: int, smem_size: int,
                          ccif_base: int) -> bool:
    """Opt-in via ``FIRMWIRE_CCCI_BRIDGE=1``; off by default.

    Reads ``FIRMWIRE_CCCI_SHM`` / ``FIRMWIRE_CCCI_SOCK`` for the file + socket
    paths. Returns True if the bridge was attached. Any failure is logged and
    swallowed so it can never break an ordinary run.
    """
    if os.environ.get("FIRMWIRE_CCCI_BRIDGE") not in ("1", "true", "yes", "on"):
        return False
    try:
        attach_ccci_bridge(
            machine,
            smem_base=smem_base, smem_size=smem_size, ccif_base=ccif_base,
            shm_path=os.environ.get("FIRMWIRE_CCCI_SHM", DEFAULT_SHM_PATH),
            sock_path=os.environ.get("FIRMWIRE_CCCI_SOCK", DEFAULT_SOCK_PATH),
        )
        return True
    except Exception:
        log.exception("CCCI bridge attach failed; continuing without it")
        return False
