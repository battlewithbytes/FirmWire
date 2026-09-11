"""Opt-in platform adapter; no CPU internals or firmware writes.

Input must come from the identity-validated CPU profile loader. The engine's
topology and core-release interface remain vendor independent.
"""
from copy import copy


def apply_boot_slave_profile(entries, profile, topology):
    devices = profile.get("platform_devices", [])
    if devices == []:
        return list(entries), None
    if not isinstance(devices, list) or len(devices) != 1:
        raise ValueError("MTK startup currently supports exactly one explicit platform device")
    device = devices[0]
    if not isinstance(device, dict) or set(device) != {"kind", "address", "size"}:
        raise ValueError("boot-slave device requires kind, address and size")
    if device["kind"] != "mtk-bootslave-analysis-v1":
        raise ValueError("unsupported platform device kind")
    address, size = device["address"], device["size"]
    if (type(address) is not int or address < 0 or address > 0xffffe000 or
            address & 0xfff or type(size) is not int or size != 0x2000):
        raise ValueError("boot-slave requires an aligned 32-bit 8 KiB MMIO range")
    if topology.cores < 2:
        raise ValueError("boot-slave requires a topology with additional cores")
    overlaps = [i for i, entry in enumerate(entries)
                if entry.start < address + size and address < entry.start + entry.size
                and entry.ty.name != "ANNOTATION"]
    if len(overlaps) != 1:
        raise ValueError("boot-slave must replace exactly one existing peripheral")
    index = overlaps[0]
    entry = entries[index]
    if (entry.start != address or entry.size != size or entry.ty.name != "PERIPHERAL" or
            entry.kwargs.get("name") != "MDPERI_MDPERISYS_MISC_REG" or
            getattr(entry.kwargs.get("emulate"), "__name__", None) != "MDPERISYS_MISC_Periph"):
        raise ValueError("boot-slave profile does not match the platform peripheral map")
    replacement = copy(entry)
    replacement.ty = entry.ty.__class__.GENERIC
    replacement.kwargs = {"name": entry.kwargs["name"], "permissions": "rw-",
                          "qemu_name": "cockpit-mtk-bootslave"}
    result = list(entries)
    result[index] = replacement
    return result, dict(kind=device["kind"], address=address, size=size,
                        qemu_name="cockpit-mtk-bootslave", experimental=True,
                        additional_core_release_wired=True, power_controller_modeled=False,
                        interrupt_controller_modeled=False,
                        ordering_policy="conservative one-shot unlock-entry-update",
                        firmware_boot_verified=False)
