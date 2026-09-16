"""Serial targets are separate from controller registers and firmware profiles.

No silicon identity or RF register defaults live here. The capture target is a
diagnostic transport sink, NOT an RF implementation or a hardware timing model.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import copy
import re


def validate_software_rf_profile(config, rom_sha256):
    """An explicit, ROM-bound experiment choice, never extracted silicon data."""
    if (not isinstance(config, dict) or config.get("schema") != "firmwire.software-rf/v1"
            or not re.fullmatch(r"[0-9a-f]{64}", str(rom_sha256))
            or config.get("rom_sha256") != rom_sha256
            or config.get("analysis_only") is not True
            or not isinstance(config.get("name"), str) or not config["name"]
            or not isinstance(config.get("assumptions"), str) or not config["assumptions"]):
        raise ValueError("Requires an explicit matching software RF analysis profile")
    ports = config.get("ports")
    if not isinstance(ports, dict) or not ports or len(ports) > 16:
        raise ValueError("Software RF profile needs explicit ports")
    for port, identity in ports.items():
        if (not isinstance(port, str) or not re.fullmatch(r"[0-9]|1[0-5]", port)
                or not isinstance(identity, dict) or set(identity) != {"chip_id", "eco"}
                or any(type(identity[k]) is not int or not 0 <= identity[k] <= 15 for k in identity)):
            raise ValueError("Invalid software RF port/identity/ECO")
    return copy.deepcopy(config)


@dataclass(frozen=True)
class SerialResult:
    status: str  # unresolved, write-complete, read-complete
    value: int = None
    reason: str = ""


class SerialTarget(ABC):
    @abstractmethod
    def exchange(self, command):
        """Handle one immutable command; unresolved must not mutate target state."""

    @abstractmethod
    def reset(self):
        pass

    @abstractmethod
    def facts(self):
        pass


class SerialBus:
    def __init__(self, targets):
        self.targets = dict(targets)
        if any(type(port) is not int or not 0 <= port < 16
               or not isinstance(target, SerialTarget) for port, target in self.targets.items()):
            raise ValueError("Serial ports require explicit target instances")

    def exchange(self, command):
        target = self.targets.get(command.port)
        if target is None:
            return SerialResult("unresolved", reason="no-target-on-port")
        return target.exchange(command)

    def reset(self):
        for target in {id(t): t for t in self.targets.values()}.values():
            target.reset()

    def facts(self):
        return {str(port): target.facts() for port, target in self.targets.items()}


@dataclass(frozen=True)
class Mt6177ControlWord:
    address: int
    payload: int

    @classmethod
    def decode(cls, word, *, read=False):
        # Family POR headers specify 10 address + 20 payload bits. The observed
        # MCU read routine instead sends a 10-bit address with bit 10 set.
        if type(word) is not int or word < 0:
            raise ValueError("Invalid serial word")
        if read:
            if word & ~0x7ff or not word & 0x400:
                raise ValueError("Unsupported MT6177 read framing")
            return cls(word & 0x3ff, None)
        if word >= 1 << 30:
            raise ValueError("Reserved MT6177 write bits are set")
        return cls(word >> 20, word & 0xfffff)


class WriteCaptureTarget(SerialTarget):
    """Explicit diagnostic substitute: capture writes, NEVER return read data.

    Does not decode or apply RF registers. Completion means recorded by this
    sink, not accepted by silicon. A real target replaces this entire object.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.writes = 0
        self.last = []

    def exchange(self, command):
        if command.extended or command.read:
            return SerialResult("unresolved", reason="capture-only-no-read-or-extended-semantics")
        self.writes += 1
        self.last.append({"sequence": command.sequence, "bank": command.bank,
                          "port": command.port, "data": tuple(command.data),
                          "lengths": tuple(command.lengths)})
        del self.last[:-16]
        return SerialResult("write-complete", reason="analysis-transport-capture-only")

    def facts(self):
        return {"kind": "write-capture-analysis-only", "rf_emulated": False,
                "silicon_verified": False, "read_values_supplied": 0,
                "timing": "synchronous-analysis-substitution",
                "writes": self.writes, "last": list(self.last)}


class SoftwareMt6177Target(SerialTarget):
    """Opt-in register-storage analysis substitute, NOT a silicon model.

    CW0 chip/ECO are caller assumptions. Only the observed CW0 reset command is
    accepted; other registers remember actual guest writes. Unwritten reads stay
    unresolved. No calibration-done bits or analog results are synthesized.
    """
    def __init__(self, chip_id, eco):
        if any(type(v) is not int or not 0 <= v <= 15 for v in (chip_id, eco)):
            raise ValueError("Explicit four-bit software RF identity/ECO required")
        self.chip_id, self.eco = chip_id, eco
        self.reset()

    def reset(self):
        self.registers = {}
        self.writes = self.reads = self.resets = 0
        self.history = []

    def exchange(self, command):
        if command.extended or not command.data:
            return SerialResult("unresolved", reason="software-rf-unsupported-transfer")
        try:
            cw = Mt6177ControlWord.decode(command.data[0], read=command.read)
        except ValueError:
            return SerialResult("unresolved", reason="software-rf-unsupported-framing")
        if command.read:
            if cw.address == 0:
                value = self.chip_id | self.eco << 4
            elif cw.address in self.registers:
                value = self.registers[cw.address]
            else:
                return SerialResult("unresolved", reason="software-rf-unwritten-register-%x" % cw.address)
            self.reads += 1
            result = SerialResult("read-complete", value, "software-register-storage-analysis")
        else:
            if cw.address == 0:
                if cw.payload != 0x80000:
                    return SerialResult("unresolved", reason="software-rf-unreviewed-cw0-write")
                self.registers.clear()
                self.resets += 1
            else:
                self.registers[cw.address] = cw.payload
            self.writes += 1
            value = cw.payload
            result = SerialResult("write-complete", reason="software-register-storage-analysis")
        self.history.append({"read": command.read, "register": cw.address, "value": value})
        del self.history[:-64]
        return result

    def facts(self):
        return {"kind": "mt6177-register-storage-analysis", "analysis_only": True,
                "silicon_verified": False, "rf_emulated": False, "calibration_emulated": False,
                "identity_source": "explicit-software-profile-assumption",
                "chip_id": self.chip_id, "eco": self.eco, "cw0": self.chip_id | self.eco << 4,
                "unknown_reads": "unresolved-not-zero-filled", "registers_written": len(self.registers),
                "writes": self.writes, "reads": self.reads, "resets": self.resets,
                "recent_transactions": list(self.history)}
