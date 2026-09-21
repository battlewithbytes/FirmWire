"""Bounded, ROM-bound opt-in analysis policy; no extracted calibration claims."""
import hashlib
import json
from .hw.abbmix import CalibrationLayout, SelectedCalibrationAnalysis, uint


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate calibration profile key")
        result[key] = value
    return result


def load_abbmix_profile(path, rom_sha256, *, boot_mode):
    with open(path, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Calibration profile exceeds 64 KiB")
    p = json.loads(raw, object_pairs_hook=_unique)
    keys = {"schema", "rom_sha256", "source", "reason", "base", "size", "layout",
            "registers", "prerequisites", "results", "modeled_range"}
    if (boot_mode != "native" or not isinstance(p, dict) or set(p) != keys
            or p["schema"] != "firmwire.abbmix-analysis/v1" or p["source"] != "analysis-assumption"
            or not isinstance(rom_sha256, str) or len(rom_sha256) != 64
            or any(c not in "0123456789abcdef" for c in rom_sha256)
            or p["rom_sha256"] != rom_sha256
            or not uint(p["base"], 32) or p["base"] % 2
            or type(p["size"]) is not int or not 2 <= p["size"] <= 65536 or p["size"] % 2
            or not isinstance(p["modeled_range"], list) or len(p["modeled_range"]) != 2
            or any(type(v) is not int or v % 2 for v in p["modeled_range"])
            or not 0 <= p["modeled_range"][0] < p["modeled_range"][1] <= p["size"]
            or not isinstance(p["layout"], dict) or set(p["layout"]) != set(CalibrationLayout.__dataclass_fields__)
            or not isinstance(p["layout"]["status"], list)
            or not isinstance(p["registers"], dict)
            or not isinstance(p["prerequisites"], list)
            or any(not uint(a, 16) for a in p["prerequisites"])
            or len(set(p["prerequisites"])) != len(p["prerequisites"])):
        raise ValueError("Invalid or mismatched calibration analysis profile")
    registers = {}
    for address, value in p["registers"].items():
        if not address.isascii() or not address.isdigit() or str(int(address)) != address:
            raise ValueError("Calibration offsets must be canonical decimal strings")
        registers[int(address)] = value
    layout = CalibrationLayout(**{**p["layout"], "status": tuple(p["layout"]["status"])})
    model = SelectedCalibrationAnalysis(layout, registers, frozenset(p["prerequisites"]),
                                        p["results"], reason=p["reason"])
    if any(not p["modeled_range"][0] <= a < a + 2 <= p["modeled_range"][1]
           for a in set(registers) | set(layout.status)):
        raise ValueError("Calibration layout exceeds declared window")
    return p, model, hashlib.sha256(raw).hexdigest()
