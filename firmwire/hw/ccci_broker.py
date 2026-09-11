## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
"""CCCI co-simulation broker: shared SMEM file + doorbell relay.

This module is the AP<->modem co-simulation glue described in
``qemu/docs/2026-09-10-cosim-architecture.md`` (section 3, "Option A" for the
shared memory window and "Option A'" for the doorbell). It is deliberately
**pure standard library** (``mmap``, ``socket``, ``struct``, ``threading``) so
it can run as its own process, be imported by the QEMU-side cockpit *and* the
FirmWire-side peripheral, and be unit-tested without avatar2 / PANDA / QEMU.

Three pieces live here:

* :class:`SharedRegion` -- an ``mmap`` of a file-backed shared-memory window.
  QEMU maps the *same* file as guest RAM (``memory-backend-file,share=on``) and
  the FirmWire :class:`~firmwire.hw.ccci_bridge.CCCISharedMemoryPeripheral`
  services the modem's SMEM window from it. Both processes then see the same
  bytes with zero copy; the CCCI ring-buffer protocol does the framing.

* :class:`DoorbellClient` -- a thin unix-socket client each emulator side uses
  to *ring* the peer's doorbell (a write to the CCIF notify register) and to be
  *notified* of the peer ringing ours (delivered to a callback on a reader
  thread). A poll-only fallback flips a flag word inside :class:`SharedRegion`
  when no broker socket is present.

* :class:`CCCIBroker` -- the process that owns the shared file and relays
  doorbell frames between the two connected peers. ``python -m
  firmwire.hw.ccci_broker`` runs it.

Doorbell fidelity is phased (see the architecture doc, section 3.3):

* **phase b -- poll-first (implemented here):** ``ring()`` delivers a
  ``(channel, value)`` frame to the peer's callback, and the callback flips a
  status/poll flag the peer's guest reads. No guest interrupt is raised.
* **phase c -- interrupt (designed, not wired):** the same callback would inject
  the peer's CCIF IRQ. On the modem that is the FirmWire/PANDA IRQ path; on the
  AP it needs an ivshmem-doorbell MSI or a custom QEMU CCIF device. That step is
  marked PENDING in the impl doc because it cannot be exercised until FirmWire
  runs on arm64/Rosetta.
"""

from __future__ import annotations

import logging
import mmap
import os
import socket
import struct
import threading
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Defaults (overridable everywhere they are used)
# --------------------------------------------------------------------------- #
# tmpfs on Linux; the same file QEMU points memory-backend-file at.
DEFAULT_SHM_PATH = "/dev/shm/ccci"
DEFAULT_SOCK_PATH = "/dev/shm/ccci.doorbell.sock"

# Size of the first shared window we bring up: the modem exception/debug SMEM
# (CCCI_EE_SMEM_TOTAL_SIZE = 64 KiB, mapped by the MTK machine as
# "RAW_MDCCCI_DBG" at SMEM_USER_RAW_MDCCCI_DBG). Kept here as a stdlib-only
# duplicate so the broker has no avatar2 import; the peripheral module and the
# profile carry the authoritative values for a given target.
DEFAULT_REGION_SIZE = 64 * 1024

# A doorbell frame is (channel, value), both little-endian u32.
_DOORBELL_FMT = "<II"
DOORBELL_FRAME_SIZE = struct.calcsize(_DOORBELL_FMT)


def pack_doorbell(channel: int, value: int) -> bytes:
    return struct.pack(_DOORBELL_FMT, channel & 0xFFFFFFFF, value & 0xFFFFFFFF)


def unpack_doorbell(data: bytes) -> "tuple[int, int]":
    return struct.unpack(_DOORBELL_FMT, data)


# --------------------------------------------------------------------------- #
# Shared memory window
# --------------------------------------------------------------------------- #
class SharedRegion:
    """A file-backed, MAP_SHARED ``mmap`` of the CCCI SMEM window.

    The backing file is created and truncated to ``size`` when ``create`` is set
    (the broker owns creation; other openers pass ``create=False`` but tolerate
    creating it too so start order does not matter). Integer accessors are
    little-endian to match the guest ABI on both aarch64 (AP) and MIPS32-LE
    (modem).
    """

    def __init__(self, path: str = DEFAULT_SHM_PATH, size: int = DEFAULT_REGION_SIZE,
                 create: bool = True) -> None:
        self.path = path
        self.size = size
        # Open read/write, creating if needed. O_CLOEXEC keeps the fd out of any
        # child emulator we might spawn.
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if st.st_size < size:
                os.ftruncate(fd, size)
            self.mem = mmap.mmap(fd, size, mmap.MAP_SHARED,
                                 mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            # mmap keeps its own reference to the mapping; the fd can be closed.
            os.close(fd)
        log.info("CCCI SharedRegion mapped %s (%#x bytes)", path, size)

    # -- byte access -------------------------------------------------------- #
    def read_bytes(self, offset: int, length: int) -> bytes:
        return bytes(self.mem[offset:offset + length])

    def write_bytes(self, offset: int, data: bytes) -> None:
        self.mem[offset:offset + len(data)] = data

    # -- little-endian integer access (mirrors PassthroughPeripheral) ------- #
    def read(self, offset: int, size: int) -> int:
        value = 0
        for i in range(size):
            value |= self.mem[offset + i] << (i * 8)
        return value

    def write(self, offset: int, size: int, value: int) -> None:
        for i in range(size):
            self.mem[offset + i] = (value >> (i * 8)) & 0xFF

    def flush(self) -> None:
        """msync the mapping so the peer is guaranteed to observe our writes."""
        try:
            self.mem.flush()
        except (ValueError, OSError):
            pass

    def close(self) -> None:
        try:
            self.mem.flush()
        finally:
            self.mem.close()


# --------------------------------------------------------------------------- #
# Doorbell client (one per emulator side)
# --------------------------------------------------------------------------- #
# A small reserved control area at the TOP of the shared window carries the
# poll-first fallback flags, so a peer with no live socket can still detect a
# ring by watching memory. Two words per direction: [seq, channel-bitmap].
POLL_AREA_WORDS = 4
POLL_AREA_SIZE = POLL_AREA_WORDS * 4


class DoorbellClient:
    """Rings the peer's doorbell and receives the peer ringing ours.

    ``ring(channel, value)`` sends a frame to the broker, which relays it to the
    peer. Incoming frames are dispatched to ``on_ring(channel, value)`` on a
    daemon reader thread. If the broker socket cannot be reached the client runs
    in **poll-only** mode: :meth:`ring` bumps a sequence word + OR's the channel
    bit into the poll area of ``region`` (if given), which the peer polls.
    """

    def __init__(self, sock_path: str = DEFAULT_SOCK_PATH,
                 region: Optional[SharedRegion] = None,
                 name: str = "peer") -> None:
        self.sock_path = sock_path
        self.region = region
        self.name = name
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._on_ring: Optional[Callable[[int, int], None]] = None
        self._running = False
        self._seq = 0
        # Offset of this side's poll word within the region's reserved area.
        self._poll_base = region.size - POLL_AREA_SIZE if region is not None else 0

    # -- connection --------------------------------------------------------- #
    def connect(self, on_ring: Optional[Callable[[int, int], None]] = None) -> bool:
        """Connect to the broker and start the reader thread.

        Returns True if the socket connected (interrupt/relay path available),
        False if we fell back to poll-only mode. Never raises on a missing
        broker -- a bridge without a live broker still works via shared memory
        polling.
        """
        self._on_ring = on_ring
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.sock_path)
            self._sock = s
            self._running = True
            if on_ring is not None:
                self._reader = threading.Thread(
                    target=self._read_loop, name=f"ccci-doorbell-{self.name}",
                    daemon=True)
                self._reader.start()
            log.info("CCCI doorbell connected to broker at %s", self.sock_path)
            return True
        except OSError as exc:
            log.warning("CCCI doorbell broker unavailable (%s); poll-only mode",
                        exc)
            self._sock = None
            return False

    def _read_loop(self) -> None:
        assert self._sock is not None
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= DOORBELL_FRAME_SIZE:
                frame, buf = buf[:DOORBELL_FRAME_SIZE], buf[DOORBELL_FRAME_SIZE:]
                channel, value = unpack_doorbell(frame)
                if self._on_ring is not None:
                    try:
                        self._on_ring(channel, value)
                    except Exception:  # a bad callback must not kill the reader
                        log.exception("CCCI doorbell callback raised")

    # -- ringing ------------------------------------------------------------ #
    def ring(self, channel: int = 0, value: int = 1) -> None:
        """Notify the peer that we put data in SMEM for ``channel``."""
        # Always flush shared memory first so the peer never sees the doorbell
        # before the payload (write ordering, architecture doc section 3.3).
        if self.region is not None:
            self.region.flush()
            self._bump_poll_flag(channel)
        if self._sock is not None:
            try:
                self._sock.sendall(pack_doorbell(channel, value))
            except OSError as exc:
                log.warning("CCCI doorbell send failed (%s)", exc)

    def _bump_poll_flag(self, channel: int) -> None:
        """Poll-first fallback: bump seq + OR the channel bit in the poll area."""
        if self.region is None:
            return
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self.region.write(self._poll_base + 0, 4, self._seq)
        prev = self.region.read(self._poll_base + 4, 4)
        self.region.write(self._poll_base + 4, 4, prev | (1 << (channel & 31)))
        self.region.flush()

    def close(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None


# --------------------------------------------------------------------------- #
# Broker process
# --------------------------------------------------------------------------- #
class CCCIBroker:
    """Owns the shared SMEM file and relays doorbell frames between peers.

    v1 is a dumb reflector: every complete frame received from one peer is
    forwarded verbatim to every *other* connected peer. That is sufficient for
    the two-peer AP<->modem topology and keeps the relay policy-free -- channel
    semantics live in the CCCI protocol inside the shared window, not here.
    """

    def __init__(self, shm_path: str = DEFAULT_SHM_PATH,
                 sock_path: str = DEFAULT_SOCK_PATH,
                 size: int = DEFAULT_REGION_SIZE) -> None:
        self.shm_path = shm_path
        self.sock_path = sock_path
        self.size = size
        self.region: Optional[SharedRegion] = None
        self._srv: Optional[socket.socket] = None
        self._clients: List[socket.socket] = []
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        # Create the shared file up front so both emulators can map it whenever
        # they come up.
        self.region = SharedRegion(self.shm_path, self.size, create=True)
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(self.sock_path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.sock_path)
        self._srv.listen(8)
        self._running = True
        log.info("CCCI broker listening on %s (shm %s, %#x bytes)",
                 self.sock_path, self.shm_path, self.size)

    def serve_forever(self) -> None:
        assert self._srv is not None
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            with self._lock:
                self._clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,),
                             daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= DOORBELL_FRAME_SIZE:
                    frame, buf = (buf[:DOORBELL_FRAME_SIZE],
                                  buf[DOORBELL_FRAME_SIZE:])
                    self._relay(frame, exclude=conn)
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            conn.close()

    def _relay(self, frame: bytes, exclude: socket.socket) -> None:
        with self._lock:
            peers = [c for c in self._clients if c is not exclude]
        for peer in peers:
            try:
                peer.sendall(frame)
            except OSError:
                pass

    def stop(self) -> None:
        self._running = False
        if self._srv is not None:
            self._srv.close()
        with self._lock:
            for c in self._clients:
                c.close()
            self._clients.clear()
        if self.region is not None:
            self.region.close()
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(self.sock_path)


def main(argv: "Optional[list[str]]" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="CCCI co-simulation broker")
    ap.add_argument("--shm-path", default=DEFAULT_SHM_PATH,
                    help="shared SMEM file (mmap'd; QEMU points memory-backend-file here)")
    ap.add_argument("--sock-path", default=DEFAULT_SOCK_PATH,
                    help="unix socket for doorbell relay")
    ap.add_argument("--size", type=lambda s: int(s, 0), default=DEFAULT_REGION_SIZE,
                    help="shared window size in bytes (accepts 0x hex)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    broker = CCCIBroker(args.shm_path, args.sock_path, args.size)
    broker.start()
    try:
        broker.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        broker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
