"""Read-only, exact-image-bound RAM observation validation (no emulator imports)."""


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
        for byte in range(address, address + 4):
            ranges = list(ranges_at(byte))
            if len(ranges) != 1:
                raise ValueError("RAM observer may not read holes or overlapping regions")
            region = ranges[0]
            data = region.data
            permissions = getattr(data, "permissions", "")
            if (region.begin > address or address + 4 > region.end or
                    getattr(data, "forwarded", True) or
                    getattr(data, "is_special", True) or
                    getattr(data, "is_symbolic", True) or
                    getattr(data, "emulate", None) is not None or
                    "r" not in permissions or "w" not in permissions):
                raise ValueError("RAM observer may only read ordinary RAM, not MMIO/ROM")
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
    markers = context.setdefault("pc_markers", {})
    sample = markers.setdefault(name, dict(hits=0, first_block=completed_blocks))
    sample["hits"] += 1
    sample["last_block"] = completed_blocks
