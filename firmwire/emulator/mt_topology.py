"""Explicit native MIPS startup topology, independent of vendor MMIO models.

This is a bounded engine capability, not an inferred hardware description.
The profile is ROM-bound; the adapter does not release additional cores.
"""
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class MipsMTTopology:
    cores: int
    vpes_per_core: int
    tcs_per_core: int

    def __post_init__(self):
        values = (self.cores, self.vpes_per_core, self.tcs_per_core)
        if any(type(value) is not int for value in values):
            raise ValueError("topology counts must be integers, not inferred/unknown values")
        if not (1 <= self.cores <= 4 and 1 <= self.vpes_per_core <= 4
                and self.vpes_per_core <= self.tcs_per_core <= 5):
            raise ValueError("topology exceeds the native startup-v1 engine capability")

    @property
    def cpu_count(self):
        return self.cores * self.vpes_per_core

    def engine_config(self):
        return dict(profile="startup-v1", cores=self.cores,
                    vpes_per_core=self.vpes_per_core, tcs_per_core=self.tcs_per_core)


def load_profile(path, rom_sha256, cpu_model):
    with open(path, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("CPU topology profile exceeds 64 KiB")
    profile = json.loads(raw)
    if not isinstance(profile, dict) or profile.get("schema") != "cockpit.mips-mt-startup/v1":
        raise ValueError("unsupported CPU topology profile schema")
    if profile.get("rom_sha256") != rom_sha256 or profile.get("cpu_model") != cpu_model:
        raise ValueError("CPU topology profile does not match this ROM and CPU model")
    if profile.get("confidence") not in ("experimental", "reviewed"):
        raise ValueError("CPU topology profile requires explicit confidence")
    evidence = profile.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(s, str) or not s for s in evidence):
        raise ValueError("CPU topology profile requires provenance evidence")
    values = profile.get("topology")
    if not isinstance(values, dict) or set(values) != {"cores", "vpes_per_core", "tcs_per_core"}:
        raise ValueError("CPU topology must explicitly specify cores, VPEs, and TCs")
    topology = MipsMTTopology(**values)
    return topology, dict(profile=profile, profile_sha256=hashlib.sha256(raw).hexdigest(),
                         engine_config=topology.engine_config(),
                         additional_core_release_wired=False, full_mt_scheduler=False,
                         firmware_boot_verified=False)


def target_class():
    # Keep profile validation usable without importing an emulator/backend.
    from avatar2.targets.pypanda_target import PyPandaTarget

    class MipsMTStartupTarget(PyPandaTarget):
        def __init__(self, *args, mt_topology, **kwargs):
            self._mt_topology = mt_topology
            additional = list(kwargs.get("additional_args") or [])
            if "-smp" in additional:
                raise ValueError("CPU count is derived from mt_topology; do not also supply -smp")
            kwargs["additional_args"] = additional + ["-smp", str(mt_topology.cpu_count)]
            super().__init__(*args, **kwargs)

        def generate_qemu_config(self):
            config = super().generate_qemu_config()
            config["mips_mt"] = self._mt_topology.engine_config()
            return config

    return MipsMTStartupTarget
