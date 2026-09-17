"""Serial targets are separate from controller registers and firmware profiles.

No silicon identity or RF register defaults live here. The capture target is a
diagnostic transport sink, NOT an RF implementation or a hardware timing model.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import copy
import re


def validate_reset_registers(registers):
    """Explicit software storage seeds, never inferred silicon reset values."""
    if not isinstance(registers, dict) or len(registers) > 64:
        raise ValueError("Software reset state must be a bounded register map")
    for address, entry in registers.items():
        if (not isinstance(address, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", address)
                or not 1 <= int(address) <= 0x3ff
                or not isinstance(entry, dict) or set(entry) != {"value", "source", "reason"}
                or type(entry["value"]) is not int or not 0 <= entry["value"] <= 0xfffff
                or entry["source"] != "analysis-assumption"
                or not isinstance(entry["reason"], str) or not entry["reason"].strip()):
            raise ValueError("Reset register requires a canonical address, 20-bit value and explicit assumption")
    return copy.deepcopy(registers)


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
                or not isinstance(identity, dict)
                or not {"chip_id", "eco"} <= set(identity) <= {"chip_id", "eco", "reset_registers", "calibration", "ldo_calibration", "rx_rc_calibration"}
                or any(type(identity[k]) is not int or not 0 <= identity[k] <= 15 for k in ("chip_id", "eco"))):
            raise ValueError("Invalid software RF port/identity/ECO")
        validate_reset_registers(identity.get("reset_registers", {}))
        if "calibration" in identity:
            validate_rcal_config(identity["calibration"])
            if set(identity.get("reset_registers", {})) & {"9", "10", "11"}:
                raise ValueError("Calibration results cannot also be reset seeds")
        if "ldo_calibration" in identity:
            validate_ldo_config(identity["ldo_calibration"])
            if "77" in identity.get("reset_registers", {}):
                raise ValueError("LDO result cannot also be a reset seed")
        if "rx_rc_calibration" in identity:
            validate_rx_rc_config(identity["rx_rc_calibration"])
            if "447" in identity.get("reset_registers", {}):
                raise ValueError("RX RC result cannot also be a reset seed")
    return copy.deepcopy(config)


def validate_rcal_config(config):
    """One reviewed digital sequence, with explicitly caller-assumed results."""
    if (not isinstance(config, dict) or set(config) != {"kind", "source", "reason", "cw10", "cw11", "trim5"}
            or config["kind"] != "mt6177m-rcal-analysis/v1"
            or config["source"] != "analysis-assumption"
            or not isinstance(config["reason"], str) or not config["reason"].strip()
            or any(type(config[k]) is not int or not 0 <= config[k] <= limit
                   for k, limit in (("cw10", 0xfffff), ("cw11", 0xfffff), ("trim5", 31)))):
        raise ValueError("Requires reviewed MT6177M RCAL kind and explicit bounded synthetic results")
    return copy.deepcopy(config)


def validate_ldo_config(config):
    if (not isinstance(config, dict) or set(config) != {"kind", "source", "reason", "trims"}
            or config["kind"] != "mt6177m-ldo-analysis/v1"
            or config["source"] != "analysis-assumption"
            or not isinstance(config["reason"], str) or not config["reason"].strip()
            or not isinstance(config["trims"], dict) or not config["trims"]):
        raise ValueError("Requires explicit reviewed MT6177M LDO analysis configuration")
    for selector, trim in config["trims"].items():
        spec = Mt6177MLdoAnalysis.SELECTORS.get(selector)
        if spec is None or type(trim) is not int or not 0 <= trim < 1 << spec[0]:
            raise ValueError("Unreviewed LDO selector or out-of-width synthetic trim")
    return copy.deepcopy(config)


def validate_rx_rc_config(config):
    if (not isinstance(config, dict) or set(config) != {"kind", "source", "reason", "trim6"}
            or config["kind"] != "mt6177m-rx-rc-analysis/v1"
            or config["source"] != "analysis-assumption"
            or not isinstance(config["reason"], str) or not config["reason"].strip()
            or type(config["trim6"]) is not int or not 0 <= config["trim6"] <= 63):
        raise ValueError("Requires reviewed MT6177M RX RC kind and explicit six-bit synthetic trim")
    return copy.deepcopy(config)


class Mt6177MRxRcAnalysis:
    """Reviewed digital setup only; no analog measurement or calibrated delay.

    CW447 supplies an assumed six-bit result after setup. Guest writeback and
    restore writes invalidate it; they do not authorize another result read.
    No firmware addresses or table writes belong in this device model.
    """
    SETUP = ((1, 0x112a0), (320, 0), (321, 0), (467, 0x2c01), (1, 0x212a8))
    CONTROLS = frozenset((1, 320, 321, 447, 467))

    def __init__(self, config):
        self.config = validate_rx_rc_config(config)
        self.reset()

    def reset(self):
        self.phase = self.completions = self.reads = 0

    def write(self, cw):
        if cw.address not in self.CONTROLS:
            return
        item = (cw.address, cw.payload)
        expected = self.SETUP[self.phase] if self.phase < len(self.SETUP) else None
        self.phase = self.phase + 1 if item == expected else 1 if item == self.SETUP[0] else 0
        if self.phase == len(self.SETUP):
            self.completions += 1

    def read(self):
        if self.phase != len(self.SETUP):
            return SerialResult("unresolved", reason="software-rx-rc-sequence-not-complete")
        self.reads += 1
        return SerialResult("read-complete", self.config["trim6"] << 14,
                            "software-rx-rc-profile-assumption")

    def facts(self):
        return {"config": copy.deepcopy(self.config), "phase": self.phase,
                "ready": self.phase == len(self.SETUP), "completions": self.completions,
                "reads": self.reads, "timing": "synchronous-on-reviewed-setup-analysis-only",
                "analog_calibration_verified": False}


class Mt6177MLdoAnalysis:
    """Reviewed digital LDO sequence; values and synchronous timing are assumed.

    Selectors map to (result width, prepare CW15, armed CW15, trigger CW75).
    The firmware clears CW76 before some reads: latch on trigger, not on read.
    No PC, image identity, polling count or analog timing drives this model.
    """
    SELECTORS = {str(s): (4 if s in (8, 0x10000) else 5,
                         0x7800 if s in (0x20, 0x40) else 0x5800,
                         0x3800 if s in (0x20, 0x40) else 0x1800,
                         0x60000 if s == 8 else 0x40000)
                 for s in (8, 0x10000, 0x100, 0x400, 0x200, 0x80, 0x800,
                           0x4000, 0x8000, 0x80000, 0x40000, 0x20000, 0x40, 0x20, 0x10)}

    def __init__(self, config):
        self.config = validate_ldo_config(config)
        self.reset()

    def reset(self):
        self.phase = "idle"
        self.prepare = self.arm = self.selector = self.latched_selector = None
        self.value = None
        self.completions = self.clear_reads = self.result_reads = 0
        self.completed_selectors = {}

    def invalidate(self):
        self.phase = "idle"
        self.prepare = self.arm = self.selector = self.latched_selector = None
        self.value = None

    def write(self, cw):
        if cw.address not in (15, 75, 76, 77):
            return
        if cw.address == 15 and cw.payload in (0x5800, 0x7800):
            self.invalidate()
            self.prepare, self.phase = cw.payload, "prepared"
        elif cw.address == 75 and cw.payload == 0x80000 and self.phase == "prepared":
            self.phase, self.value = "cleared", 0  # explicitly synthetic clear-phase response
        elif (cw.address == 15 and self.phase == "cleared"
              and cw.payload == self.prepare - 0x4000):
            self.arm, self.phase, self.value = cw.payload, "armed", None
        elif cw.address == 76 and self.phase == "armed":
            self.selector = str(cw.payload)
        elif cw.address == 75 and self.phase == "armed":
            spec = self.SELECTORS.get(self.selector)
            if (self.selector in self.config["trims"] and spec is not None
                    and (self.prepare, self.arm, cw.payload) == spec[1:]):
                self.latched_selector = self.selector
                self.value = self.config["trims"][self.selector] << 15
                self.phase = "complete"
                self.completions += 1
                self.completed_selectors[self.selector] = self.completed_selectors.get(self.selector, 0) + 1
            else:
                self.invalidate()
        elif cw.address == 76 and cw.payload == 0 and self.phase == "complete":
            self.selector = None  # RX clears selection before consuming the latched result
        else:
            self.invalidate()

    def read(self):
        if self.phase not in ("cleared", "complete"):
            return SerialResult("unresolved", reason="software-ldo-sequence-not-complete")
        if self.phase == "cleared":
            self.clear_reads += 1
        else:
            self.result_reads += 1
        return SerialResult("read-complete", self.value, "software-ldo-profile-assumption")

    def facts(self):
        return {"config": copy.deepcopy(self.config), "phase": self.phase,
                "latched_selector": self.latched_selector, "completions": self.completions,
                "clear_reads": self.clear_reads, "result_reads": self.result_reads,
                "completed_selectors": dict(self.completed_selectors),
                "timing": "synchronous-on-reviewed-trigger-analysis-only",
                "clear_phase_value": 0, "analog_calibration_verified": False}


class Mt6177MRcalAnalysis:
    """Synchronous software RCAL results, not analog measurements or timing.

    Only the reviewed MT6177M control sequence authorizes result reads. No PC,
    image name, poll count or chipset heuristic changes the device response.
    Other calibration stages/variants remain unsupported.
    """
    SETUP = ((8, 0x81c00), (8, 0x81c01), (9, 0), (12, 0))
    RESULTS = frozenset((9, 10, 11))

    def __init__(self, config):
        self.config = validate_rcal_config(config)
        self.reset()

    def reset(self):
        self.phase = 0
        self.completions = 0
        self.reads = 0

    def write(self, cw):
        # Any write to result/control registers is a new operation or invalidates
        # the old one. In particular, Coful's extra CW11 write cannot reuse this
        # variant's completed results. Unrelated trim writes leave results stable.
        if cw.address not in (8, 9, 10, 11, 12):
            return
        item = (cw.address, cw.payload)
        expected = self.SETUP[self.phase] if self.phase < len(self.SETUP) else None
        self.phase = self.phase + 1 if item == expected else 1 if item == self.SETUP[0] else 0
        if self.phase == len(self.SETUP):
            self.completions += 1

    def read(self, address):
        if address not in self.RESULTS:
            raise ValueError("Not an RCAL result register")
        if self.phase != len(self.SETUP):
            return SerialResult("unresolved", reason="software-rcal-sequence-not-complete")
        value = (self.config["trim5"] << 10 if address == 9 else
                 self.config["cw10"] if address == 10 else self.config["cw11"])
        self.reads += 1
        return SerialResult("read-complete", value, "software-rcal-profile-assumption")

    def facts(self):
        return {"config": copy.deepcopy(self.config), "phase": self.phase,
                "ready": self.phase == len(self.SETUP), "completions": self.completions,
                "reads": self.reads, "timing": "synchronous-on-reviewed-setup-analysis-only",
                "analog_calibration_verified": False}


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
    accepted; other registers remember actual guest writes or explicitly
    declared software reset seeds. Optional calibration models supply only
    explicitly assumed results after their reviewed command sequence.
    """
    def __init__(self, chip_id, eco, reset_registers=None, calibration=None, ldo_calibration=None,
                 rx_rc_calibration=None):
        if any(type(v) is not int or not 0 <= v <= 15 for v in (chip_id, eco)):
            raise ValueError("Explicit four-bit software RF identity/ECO required")
        self.chip_id, self.eco = chip_id, eco
        self.reset_registers = validate_reset_registers({} if reset_registers is None else reset_registers)
        self.calibration = Mt6177MRcalAnalysis(calibration) if calibration is not None else None
        self.ldo_calibration = Mt6177MLdoAnalysis(ldo_calibration) if ldo_calibration is not None else None
        self.rx_rc_calibration = Mt6177MRxRcAnalysis(rx_rc_calibration) if rx_rc_calibration is not None else None
        if self.rx_rc_calibration and "447" in self.reset_registers:
            raise ValueError("RX RC result cannot also be a reset seed")
        if self.ldo_calibration and "77" in self.reset_registers:
            raise ValueError("LDO result cannot also be a reset seed")
        if self.calibration and set(self.reset_registers) & {"9", "10", "11"}:
            raise ValueError("Calibration results cannot also be reset seeds")
        self.reset()

    def _reset_storage(self):
        self.registers = {int(address): entry["value"] for address, entry in self.reset_registers.items()}
        self.written = set()
        if self.calibration:
            self.calibration.reset()
        if self.ldo_calibration:
            self.ldo_calibration.reset()
        if self.rx_rc_calibration:
            self.rx_rc_calibration.reset()

    def reset(self):
        self._reset_storage()
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
            calibrated = None
            if cw.address == 0:
                value = self.chip_id | self.eco << 4
            elif self.rx_rc_calibration and cw.address == 447:
                calibrated = self.rx_rc_calibration.read()
                if calibrated.status == "unresolved":
                    return calibrated
                value = calibrated.value
            elif self.ldo_calibration and cw.address == 77:
                calibrated = self.ldo_calibration.read()
                if calibrated.status == "unresolved":
                    return calibrated
                value = calibrated.value
            elif self.calibration and cw.address in self.calibration.RESULTS:
                calibrated = self.calibration.read(cw.address)
                if calibrated.status == "unresolved":
                    return calibrated
                value = calibrated.value
            elif cw.address in self.registers:
                value = self.registers[cw.address]
            else:
                return SerialResult("unresolved", reason="software-rf-unwritten-register-%x" % cw.address)
            self.reads += 1
            reason = ("software-reset-profile-assumption" if cw.address != 0 and cw.address not in self.written
                      else "software-register-storage-analysis")
            result = calibrated or SerialResult("read-complete", value, reason)
        else:
            if cw.address == 0:
                if cw.payload != 0x80000:
                    return SerialResult("unresolved", reason="software-rf-unreviewed-cw0-write")
                self._reset_storage()
                self.resets += 1
            else:
                self.registers[cw.address] = cw.payload
                self.written.add(cw.address)
                if self.calibration:
                    self.calibration.write(cw)
                if self.ldo_calibration:
                    self.ldo_calibration.write(cw)
                if self.rx_rc_calibration:
                    self.rx_rc_calibration.write(cw)
            self.writes += 1
            value = cw.payload
            result = SerialResult("write-complete", reason="software-register-storage-analysis")
        self.history.append({"read": command.read, "register": cw.address, "value": value})
        del self.history[:-64]
        return result

    def facts(self):
        return {"kind": "mt6177-register-storage-analysis", "analysis_only": True,
                "silicon_verified": False, "rf_emulated": False,
                "calibration_emulated": any(m is not None for m in
                    (self.calibration, self.ldo_calibration, self.rx_rc_calibration)),
                "calibration": self.calibration.facts() if self.calibration else None,
                "ldo_calibration": self.ldo_calibration.facts() if self.ldo_calibration else None,
                "rx_rc_calibration": self.rx_rc_calibration.facts() if self.rx_rc_calibration else None,
                "identity_source": "explicit-software-profile-assumption",
                "chip_id": self.chip_id, "eco": self.eco, "cw0": self.chip_id | self.eco << 4,
                "unknown_reads": "unresolved-not-zero-filled", "registers_written": len(self.written),
                "reset_registers": copy.deepcopy(self.reset_registers),
                "writes": self.writes, "reads": self.reads, "resets": self.resets,
                "recent_transactions": list(self.history)}
