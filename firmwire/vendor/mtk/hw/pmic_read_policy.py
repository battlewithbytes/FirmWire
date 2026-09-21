"""Explicit bounded PMIC read substitution for software-only analysis.

This is not a silicon reset table. Reads do not allocate registers, enable
writes, or override explicit unreadable/unknown register policies.
"""
from dataclasses import asdict, dataclass

from .pmic import PmicResult, PmicTarget, uint


@dataclass(frozen=True)
class PmicReadPolicy:
    start: int
    end: int
    stride: int
    value: int
    reason: str

    def __post_init__(self):
        if (not uint(self.start, 16) or not uint(self.end, 16) or self.start > self.end
                or type(self.stride) is not int or self.stride not in (1, 2, 4)
                or (self.end - self.start) % self.stride or not uint(self.value, 16)
                or not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("Invalid explicit bounded PMIC read policy")

    def contains(self, address):
        return (uint(address, 16) and self.start <= address <= self.end
                and (address - self.start) % self.stride == 0)


class PmicReadPolicyAnalysis(PmicTarget):
    """Compose a strict target with declared synthetic reads, never write fallback."""
    def __init__(self, target, explicit_addresses, policy):
        if (not isinstance(target, PmicTarget) or not isinstance(policy, PmicReadPolicy)
                or not isinstance(explicit_addresses, (set, frozenset))
                or any(not uint(a, 16) for a in explicit_addresses)):
            raise ValueError("Read policy requires an explicit target and address ownership")
        self.target = target
        self.explicit_addresses = frozenset(explicit_addresses)
        self.policy = policy
        self.substituted_reads = 0
        self.recent_addresses = []

    def read(self, address):
        if uint(address, 16) and address not in self.explicit_addresses and self.policy.contains(address):
            self.substituted_reads += 1
            self.recent_addresses.append(address)
            del self.recent_addresses[:-16]
            return PmicResult("read-complete", self.policy.value, "explicit-pmic-read-substitution")
        return self.target.read(address)

    def write(self, address, value):
        return self.target.write(address, value)

    def reset(self):
        self.target.reset()
        self.substituted_reads = 0
        self.recent_addresses.clear()

    def facts(self):
        return dict(kind="bounded-pmic-read-analysis/v1", source="analysis-assumption",
                    silicon_verified=False, reset_values_verified=False,
                    policy=asdict(self.policy), substituted_reads=self.substituted_reads,
                    recent_addresses=list(self.recent_addresses), write_fallback=False,
                    explicit_registers_override_policy=True, device=self.target.facts())
