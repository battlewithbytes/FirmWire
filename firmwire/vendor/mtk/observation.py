"""Read-only, exact-image-bound RAM observation validation (no emulator imports)."""


def _validate_ram_span(address, size, ranges_at):
    for byte in range(address, address + size):
        ranges = list(ranges_at(byte))
        if len(ranges) != 1:
            raise ValueError("RAM observer may not read holes or overlapping regions")
        region = ranges[0]
        data = region.data
        permissions = getattr(data, "permissions", "")
        if (region.begin > address or address + size > region.end or
                getattr(data, "forwarded", True) or
                getattr(data, "is_special", True) or
                getattr(data, "is_symbolic", True) or
                getattr(data, "emulate", None) is not None or
                "r" not in permissions or "w" not in permissions):
            raise ValueError("RAM observer may only read ordinary RAM, not MMIO/ROM")


def validate_ram_observer(config, rom_sha256, ranges_at):
    """Reject anything not unambiguously ordinary readable/writable RAM.

    ``ranges_at`` returns Avatar interval objects. Validate every byte so an
    overlapping MMIO interval cannot hide inside an otherwise valid word.
    """
    if (not isinstance(config, dict) or
            config.get("schema") != "cockpit.mtk-ram-observer/v1" or
            config.get("rom_sha256") != rom_sha256):
        raise ValueError("RAM observer image identity mismatch")
    words = config.get("words")
    if not isinstance(words, list) or not 1 <= len(words) <= 16:
        raise ValueError("RAM observer requires 1..16 word addresses")
    for address in words:
        if type(address) is not int or not 0 <= address <= 0xfffffffc or address % 4:
            raise ValueError("RAM observer requires aligned 32-bit integer addresses")
        _validate_ram_span(address, 4, ranges_at)
    if len(set(words)) != len(words):
        raise ValueError("RAM observer contains duplicate words")
    return list(words)


def validate_pc_markers(config):
    """Optional exact TB entry markers; call after ROM-bound RAM validation.

    Labels describe observation points, never independently certify tasks.
    No register or guest-memory access is performed by this observer.
    """
    markers = config.get("pc_markers", {})
    if not isinstance(markers, dict) or len(markers) > 16:
        raise ValueError("PC observer supports at most 16 named entry markers")
    for name, pc in markers.items():
        if not isinstance(name, str) or not name.isidentifier() or len(name) > 64:
            raise ValueError("PC marker names must be short identifiers")
        if type(pc) is not int or not 0 <= pc <= 0xfffffffe or pc & 1:
            raise ValueError("PC markers require even 32-bit TB entry addresses")
    if len(set(markers.values())) != len(markers):
        raise ValueError("PC marker addresses must be distinct")
    return {pc: name for name, pc in markers.items()}


def record_pc_marker(context, name, completed_blocks):
    """Return true only on the first hit, allowing a bounded durable checkpoint."""
    markers = context.setdefault("pc_markers", {})
    sample = markers.setdefault(name, dict(hits=0, first_block=completed_blocks))
    sample["hits"] += 1
    sample["last_block"] = completed_blocks
    return sample["hits"] == 1


class RamObservation:
    """Fixed-address, ROM-bound snapshots; no firmware semantics or pointer chasing.

    The adapter supplies the memory map and byte reader. Only explicitly
    approved ordinary RAM words are read; unknown mappings fail before reads.
    """

    def __init__(self, config, rom_sha256, ranges_at, read_bytes):
        self.words = validate_ram_observer(config, rom_sha256, ranges_at)
        windows = config.get("byte_windows", {})
        if not isinstance(windows, dict) or len(windows) > 4:
            raise ValueError("RAM observer permits at most four byte windows")
        self.byte_windows = {}
        for name, span in windows.items():
            if (not isinstance(name, str) or not name.isidentifier() or len(name) > 64
                    or not isinstance(span, dict) or set(span) != {"address", "size"}):
                raise ValueError("RAM byte windows require named address/size pairs")
            address, size = span["address"], span["size"]
            if (type(address) is not int or type(size) is not int or address < 0
                    or not 4 <= size <= 256 or address % 4 or size % 4
                    or address + size > 2**32):
                raise ValueError("RAM byte windows require aligned bounded 32-bit spans")
            _validate_ram_span(address, size, ranges_at)
            for previous in self.byte_windows.values():
                if address < previous["address"] + previous["size"] and address + size > previous["address"]:
                    raise ValueError("RAM byte windows may not overlap")
            self.byte_windows[name] = dict(span)
        self._read_bytes = read_bytes

    def sample(self, completed_blocks):
        words = {}
        for address in self.words:
            value = self._read_bytes(address, 4)
            if len(value) != 4:
                raise ValueError("RAM observer received a short word read")
            words[hex(address)] = int.from_bytes(value, "little")
        snapshot = {"completed_blocks": completed_blocks, "words": words}
        if self.byte_windows:
            windows = {}
            for name, span in self.byte_windows.items():
                data = self._read_bytes(span["address"], span["size"])
                if len(data) != span["size"]:
                    raise ValueError("RAM observer received a short byte-window read")
                windows[name] = dict(span, hex=bytes(data).hex())
            snapshot["byte_windows"] = windows
        return snapshot
