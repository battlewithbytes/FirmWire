"""Transport-independent PMIC contract and explicit digital storage experiment.

No addresses, chip IDs, reset defaults, oscillator or power behavior are inferred.
The composition owner resets a shared target; individual transports must not.
"""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


def uint(value, bits):
    return type(value) is int and 0 <= value < (1 << bits)


@dataclass(frozen=True)
class PmicResult:
    status: str
    value: int = None
    reason: str = ""


def check_result(result, *, read):
    if not isinstance(result, PmicResult):
        raise ValueError("PMIC target returned an invalid result")
    if result.status == "unresolved" and result.value is None:
        return result
    if read and result.status == "read-complete" and uint(result.value, 16):
        return result
    if not read and result.status == "write-complete" and result.value is None:
        return result
    raise ValueError("PMIC target returned an invalid completion")


class PmicTarget(ABC):
    @abstractmethod
    def read(self, address):
        """Unresolved requests must not mutate target state."""

    @abstractmethod
    def write(self, address, value):
        """Complete only a supported operation; never invent missing state."""

    @abstractmethod
    def reset(self):
        """Whole-device reset, called explicitly by the composition owner."""

    @abstractmethod
    def facts(self):
        pass


@dataclass(frozen=True)
class PmicRegisterSpec:
    reset_value: int = None
    write_mask: int = 0xffff
    readable: bool = True

    def __post_init__(self):
        if (self.reset_value is not None and not uint(self.reset_value, 16)
                or not uint(self.write_mask, 16) or type(self.readable) is not bool):
            raise ValueError("Invalid explicit PMIC register policy")


class PmicRegisterMapAnalysis(PmicTarget):
    """Only caller-selected plain registers; masks preserve unwritable bits.

    None means unknown, not zero. Partial writes to an unknown word stay
    unresolved. Aliases, protection keys and analog side effects are unsupported.
    """
    def __init__(self, registers, *, reason):
        if (not isinstance(registers, dict) or not 1 <= len(registers) <= 256
                or any(not uint(a, 16) or not isinstance(s, PmicRegisterSpec)
                       for a, s in registers.items())
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("PMIC storage needs explicit bounded registers and an assumption reason")
        self._specs = dict(registers)
        self.reason = reason
        self.reset()

    def reset(self):
        self._values = {a: s.reset_value for a, s in self._specs.items()}
        self.reads = self.writes = 0

    def read(self, address):
        spec = self._specs.get(address) if uint(address, 16) else None
        if spec is None or not spec.readable or self._values[address] is None:
            return PmicResult("unresolved", reason="pmic-register-read-unknown")
        self.reads += 1
        return PmicResult("read-complete", self._values[address], "explicit-pmic-storage-analysis")

    def write(self, address, value):
        spec = self._specs.get(address) if uint(address, 16) else None
        if spec is None or not uint(value, 16) or not spec.write_mask:
            return PmicResult("unresolved", reason="pmic-register-write-unsupported")
        previous = self._values[address]
        if previous is None and spec.write_mask != 0xffff:
            return PmicResult("unresolved", reason="pmic-preserved-bits-unknown")
        self._values[address] = ((previous or 0) & ~spec.write_mask) | (value & spec.write_mask)
        self.writes += 1
        return PmicResult("write-complete", reason="explicit-pmic-storage-analysis")

    def facts(self):
        return dict(kind="pmic-register-map-analysis/v1", source="analysis-assumption",
                    reason=self.reason, silicon_verified=False, analog_behavior_modelled=False,
                    protection_modelled=False, timing="synchronous-analysis-substitution",
                    reads=self.reads, writes=self.writes,
                    registers={str(a): {**asdict(s), "value": self._values[a]}
                               for a, s in sorted(self._specs.items())})


class PmicWacsControl:
    """Strict opt-in WACS transport. Polling never retries or supplies data.

    FSM 0=idle, 2=request pending, 6=read valid until explicit clear. The layout
    is the reviewed legacy WACS word encoding, not a universal PMIC protocol.
    """
    def __init__(self, target, *, init_done_offset=21):
        if (not isinstance(target, PmicTarget) or type(init_done_offset) is not int
                or not 21 <= init_done_offset < 32):
            raise ValueError("WACS requires a PMIC target and a nonoverlapping init bit")
        self.target, self.init_done_offset = target, init_done_offset
        self.reset()

    def reset(self):
        self.state = self.data = self.completed_reads = self.completed_writes = 0
        self.pending = None
        self.blocker = None

    def read(self, offset):
        if type(offset) is not int or offset != 4:
            raise ValueError("Unsupported strict WACS read register")
        return self.data | self.state << 16 | 1 << self.init_done_offset

    def write(self, offset, value):
        if type(offset) is not int or not uint(value, 32):
            raise ValueError("Invalid strict WACS word access")
        if offset == 8:
            if value != 1 or self.state != 6:
                raise ValueError("WACS valid clear requires a completed read and value one")
            self.state = self.data = 0
            return
        if offset != 0 or self.state != 0:
            raise ValueError("WACS command register unavailable")
        # Write flag is not part of the 15-bit, halfword-address field.
        self.pending = dict(read=not bool(value >> 31),
                            address=((value >> 16) & 0x7fff) << 1, value=value & 0xffff)
        self.state = 2
        self.retry_pending()

    def retry_pending(self):
        """Explicit backend retry only; no progress driven by guest polls."""
        if self.pending is None:
            raise ValueError("No pending WACS request")
        request = self.pending
        read = request["read"]
        reply = (self.target.read(request["address"]) if read else
                 self.target.write(request["address"], request["value"]))
        reply = check_result(reply, read=read)
        if reply.status == "unresolved":
            self.blocker = reply.reason
            return False
        self.pending = self.blocker = None
        if read:
            self.data, self.state = reply.value, 6
            self.completed_reads += 1
        else:
            self.state = self.data = 0
            self.completed_writes += 1
        return True

    def facts(self):
        return dict(kind="shared-pmic-wacs-analysis/v1", state=self.state, data=self.data,
                    pending=dict(self.pending) if self.pending is not None else None,
                    blocker=self.blocker, completed_reads=self.completed_reads,
                    completed_writes=self.completed_writes, polling_advances=False)
