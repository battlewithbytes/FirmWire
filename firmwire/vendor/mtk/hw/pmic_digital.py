"""Profile-driven PMIC SET/CLR and keyed-write digital analysis.

No chip addresses, regulator readiness, oscillator behavior or reset effects.
Key membership/initial state are explicit analysis policies, not silicon claims.
"""
from dataclasses import dataclass

from .pmic import PmicRegisterMapAnalysis, PmicResult, uint


@dataclass(frozen=True)
class PmicAliasSpec:
    target: int
    operation: str

    def __post_init__(self):
        if not uint(self.target, 16) or self.operation not in ("set", "clear"):
            raise ValueError("Invalid PMIC SET/CLR alias")


@dataclass(frozen=True)
class PmicKeySpec:
    address: int
    unlock_value: int
    lock_value: int
    protected: frozenset
    reason: str

    def __post_init__(self):
        if (any(not uint(v, 16) for v in (self.address, self.unlock_value, self.lock_value))
                or self.unlock_value == self.lock_value
                or not isinstance(self.protected, frozenset) or not 1 <= len(self.protected) <= 256
                or any(not uint(a, 16) for a in self.protected) or self.address in self.protected
                or not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("Invalid explicit PMIC key policy")


class PmicDigitalAnalysis(PmicRegisterMapAnalysis):
    def __init__(self, registers, *, aliases, key=None, reject_write_bits=None, reason):
        super().__init__(registers, reason=reason)
        if (not isinstance(aliases, dict) or len(aliases) > 256
                or any(not uint(a, 16) or a in registers or not isinstance(s, PmicAliasSpec)
                       or s.target not in registers for a, s in aliases.items())):
            raise ValueError("PMIC aliases require distinct addresses and existing ordinary targets")
        if key is not None and (not isinstance(key, PmicKeySpec) or key.address not in registers
                or registers[key.address].write_mask != 0 or not key.protected <= registers.keys()
                or any(s.target == key.address for s in aliases.values())):
            raise ValueError("PMIC key must have explicit read policy, no ordinary writes, and valid protected registers")
        rejected = {} if reject_write_bits is None else reject_write_bits
        if (not isinstance(rejected, dict) or len(rejected) > 256
                or any(not uint(a, 16) or a not in registers or not uint(bits, 16) or not bits
                       for a, bits in rejected.items())
                or key is not None and key.address in rejected):
            raise ValueError("Unsupported-write masks require existing non-key registers")
        self.aliases = dict(aliases)
        self.key = key
        self.reject_write_bits = dict(rejected)

    def reset(self):
        super().reset()
        self.unlocked = False
        self.key_writes = self.alias_writes = 0
        self.recent_writes = []

    def _record(self, address, value, target, operation):
        self.recent_writes.append(dict(address=address, value=value, target=target, operation=operation))
        del self.recent_writes[:-32]

    def write(self, address, value):
        if not uint(address, 16) or not uint(value, 16):
            return PmicResult("unresolved", reason="pmic-invalid-write")
        if self.key is not None and address == self.key.address:
            if value not in (self.key.unlock_value, self.key.lock_value):
                return PmicResult("unresolved", reason="pmic-key-value-unsupported")
            self.unlocked = value == self.key.unlock_value
            self.key_writes += 1
            self.writes += 1
            self._record(address, value, address, "unlock" if self.unlocked else "lock")
            return PmicResult("write-complete", reason="explicit-pmic-key-analysis")
        alias = self.aliases.get(address)
        destination = alias.target if alias else address
        if self.key is not None and destination in self.key.protected and not self.unlocked:
            return PmicResult("unresolved", reason="pmic-protected-write-locked")
        if value & self.reject_write_bits.get(destination, 0):
            return PmicResult("unresolved", reason="pmic-write-side-effect-unsupported")
        if alias:
            previous = self._values[destination]
            spec = self._specs[destination]
            if previous is None or not spec.write_mask:
                return PmicResult("unresolved", reason="pmic-alias-state-unknown-or-readonly")
            effective = value & spec.write_mask
            self._values[destination] = previous | effective if alias.operation == "set" else previous & ~effective
            self.writes += 1
            self.alias_writes += 1
            self._record(address, value, destination, alias.operation)
            return PmicResult("write-complete", reason="explicit-pmic-alias-analysis")
        result = super().write(address, value)
        if result.status == "write-complete":
            self._record(address, value, address, "store")
        return result

    def facts(self):
        facts = super().facts()
        facts.update(kind="pmic-digital-register-analysis/v1", protection_modelled=self.key is not None,
                     protection_semantics_verified=False, key_unlocked=self.unlocked,
                     key_writes=self.key_writes, alias_writes=self.alias_writes,
                     aliases={str(a): dict(target=s.target, operation=s.operation) for a,s in sorted(self.aliases.items())},
                     rejected_write_bits={str(a): bits for a,bits in sorted(self.reject_write_bits.items())},
                     recent_writes=[dict(e) for e in self.recent_writes])
        return facts
