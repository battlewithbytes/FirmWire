"""Explicit PMIC analysis selection; no firmware-derived device identity claims."""
import copy
from dataclasses import dataclass
import hashlib
import json
import re

from .hw.pmic import PmicRegisterMapAnalysis, PmicRegisterSpec


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate PMIC profile key: " + key)
        result[key] = value
    return result


@dataclass
class PmicAnalysisBinding:
    profile: dict
    profile_sha256: str
    target: PmicRegisterMapAnalysis

    @property
    def wrapper_name(self):
        return self.profile["wrapper_name"]

    @property
    def bsi_port(self):
        return self.profile["bsi_port"]

    def facts(self):
        return dict(profile=copy.deepcopy(self.profile), profile_sha256=self.profile_sha256,
                    hardware_family_selected=None, silicon_verified=False, boot_verified=False,
                    unknown_access_policy="unresolved", shared_target=True)


def load_pmic_analysis(path, rom_sha256, *, boot_mode, bsi_mode):
    if boot_mode != "native" or bsi_mode not in ("mt6768-pending", "mt6768-software-rf"):
        raise ValueError("PMIC analysis requires native boot and pending/software-RF BSI")
    with open(path, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("PMIC profile exceeds 64 KiB")
    config = json.loads(raw, object_pairs_hook=_unique_object)
    keys = {"schema", "kind", "rom_sha256", "name", "source", "reason", "wrapper_name", "bsi_port", "registers"}
    if (not isinstance(config, dict) or set(config) != keys
            or config["schema"] != "firmwire.pmic-analysis/v1"
            or config["kind"] != "plain-register-map-analysis/v1"
            or not isinstance(rom_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", rom_sha256)
            or config["rom_sha256"] != rom_sha256
            or config["source"] != "analysis-assumption"
            or any(not isinstance(config[k], str) or not config[k].strip() for k in ("name", "reason"))
            or not isinstance(config["wrapper_name"], str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", config["wrapper_name"])
            or type(config["bsi_port"]) is not int or not 0 <= config["bsi_port"] < 16
            or not isinstance(config["registers"], dict) or not 1 <= len(config["registers"]) <= 256):
        raise ValueError("Invalid or mismatched explicit PMIC analysis profile")
    registers = {}
    for address, entry in config["registers"].items():
        if (not re.fullmatch(r"0|[1-9][0-9]{0,4}", address) or int(address) > 65535
                or not isinstance(entry, dict)
                or set(entry) != {"reset_value", "write_mask", "readable", "reason"}
                or not isinstance(entry["reason"], str) or not entry["reason"].strip()):
            raise ValueError("PMIC register needs a canonical address and explicit storage policy")
        registers[int(address)] = PmicRegisterSpec(entry["reset_value"], entry["write_mask"], entry["readable"])
    target = PmicRegisterMapAnalysis(registers, reason=config["reason"])
    return PmicAnalysisBinding(copy.deepcopy(config), hashlib.sha256(raw).hexdigest(), target)


def select_wrapper(binding, peripherals):
    """Resolve exactly one existing SoC wrapper; never guess an MMIO mapping."""
    from .hw.PMICPeripheral import PMIC_WRAP_Periph
    matches = [p for p in peripherals if p._attr.get("name") == binding.wrapper_name]
    if len(matches) != 1 or not issubclass(matches[0]._cls, PMIC_WRAP_Periph):
        raise ValueError("PMIC profile must select exactly one existing PMIC wrapper")
    return matches[0]
