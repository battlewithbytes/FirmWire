"""Selected-result calibration analysis, not analog hardware emulation.

No firmware PCs, chipset addresses, or measured calibration defaults. Completion
is a declared synchronous software-backend policy; reads never advance work.
"""
from copy import deepcopy
from dataclasses import dataclass


def uint(value, bits):
    return type(value) is int and 0 <= value < 1 << bits


@dataclass(frozen=True)
class CalibrationLayout:
    trigger: int
    selector: int
    status: tuple
    trigger_mask: int
    ready_mask: int
    result_bits: int
    selector_shift: int
    selector_bits: int

    def __post_init__(self):
        if not isinstance(self.status, tuple) or any(not uint(v, 16) for v in self.status):
            raise ValueError("Invalid calibration status offsets")
        offsets = (self.trigger, self.selector) + self.status
        if (not 1 <= len(self.status) <= 4 or any(not uint(v, 16) or v % 2 for v in offsets)
                or len(set(offsets)) != len(offsets)
                or not uint(self.trigger_mask, 16) or self.trigger_mask == 0
                or self.trigger_mask & (self.trigger_mask - 1)
                or not uint(self.ready_mask, 16) or self.ready_mask == 0
                or self.ready_mask & (self.ready_mask - 1)
                or type(self.result_bits) is not int or not 1 <= self.result_bits <= 15
                or self.ready_mask < 1 << self.result_bits
                or type(self.selector_bits) is not int or not 1 <= self.selector_bits <= 6
                or type(self.selector_shift) is not int or not 0 <= self.selector_shift <= 15
                or self.selector_shift + self.selector_bits > 16):
            raise ValueError("Invalid calibration register layout")


class SelectedCalibrationAnalysis:
    def __init__(self, layout, registers, prerequisites, results, *, reason):
        if not isinstance(layout, CalibrationLayout) or not isinstance(registers, dict):
            raise ValueError("Explicit calibration layout/registers required")
        if (not 2 <= len(registers) <= 64
                or any(not uint(a, 16) or a % 2 or (v is not None and not uint(v, 16))
                       for a, v in registers.items())
                or set(layout.status) & registers.keys()
                or not {layout.trigger, layout.selector} <= registers.keys()
                or registers[layout.trigger] is None or registers[layout.trigger] & layout.trigger_mask
                or registers[layout.selector] is None
                or not isinstance(prerequisites, frozenset)
                or not prerequisites <= registers.keys()
                or any(type(a) is not int for a in prerequisites)
                or prerequisites & {layout.trigger, layout.selector}
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("Invalid explicit calibration storage/initial state")
        count = 1 << layout.selector_bits
        if results is not None and (not isinstance(results, (list, tuple))
                or len(results) != len(layout.status)
                or any(not isinstance(bank, (list, tuple)) or len(bank) != count
                       or any(not uint(v, layout.result_bits) for v in bank) for bank in results)):
            raise ValueError("Calibration needs a full width-bounded result bank per status register")
        self.layout = layout
        self.initial = dict(registers)
        self.prerequisites = prerequisites
        self.backend = None if results is None else tuple(tuple(bank) for bank in results)
        self.reason = reason
        self.reset()

    def reset(self):
        self.values = dict(self.initial)
        self.written = set()
        self.latched = None
        self.ready = False
        self.pending = False
        self.triggers = self.completions = 0
        self.reads = self.writes = 0
        self.last_unsupported = None
        self.recent = []
        self.result_reads = [[0] * (1 << self.layout.selector_bits) for _ in self.layout.status]

    def _access(self, offset, size, value=None):
        if not uint(offset, 16) or offset % 2 or size != 2 or type(size) is not int:
            raise ValueError("Calibration supports aligned halfword accesses only")
        if value is not None and not uint(value, 16):
            raise ValueError("Invalid calibration halfword")

    def _unsupported(self, direction, offset, reason):
        self.last_unsupported = dict(direction=direction, offset=offset, reason=reason)
        raise NotImplementedError(reason)

    def _record(self, direction, offset, value):
        self.recent.append(dict(direction=direction, offset=offset, value=value))
        del self.recent[:-32]

    def read(self, offset, size):
        self._access(offset, size)
        if offset in self.layout.status:
            bank = self.layout.status.index(offset)
            index = (self.values[self.layout.selector] >> self.layout.selector_shift) & ((1 << self.layout.selector_bits) - 1)
            # Invalid data is explicit zero with ready clear; not a completion.
            value = 0 if self.latched is None else self.latched[bank][index]
            if self.ready:
                value |= self.layout.ready_mask
                self.result_reads[bank][index] += 1
        elif offset in self.values and self.values[offset] is not None:
            value = self.values[offset]
        else:
            return self._unsupported("read", offset, "unknown-calibration-register-or-state")
        self.reads += 1
        self._record("read", offset, value)
        return value

    def write(self, offset, size, value):
        if not uint(value, 16):
            raise ValueError("Invalid calibration halfword")
        self._access(offset, size, value)
        if offset not in self.values:
            return self._unsupported("write", offset, "unsupported-calibration-register")
        old = self.values[offset]
        rising = offset == self.layout.trigger and not old & self.layout.trigger_mask and value & self.layout.trigger_mask
        if rising:
            self.triggers += 1
            self.latched = None
            self.ready = False
            self.pending = True
            if self.prerequisites <= self.written and self.backend is not None:
                self.latched = self.backend
                self.ready = True
                self.pending = False
                self.completions += 1
        elif offset == self.layout.trigger and not value & self.layout.trigger_mask:
            # Explicit analysis policy: stop cancels pending work, but completed
            # results/ready survive for the firmware's subsequent selected reads.
            self.pending = False
        self.values[offset] = value
        self.written.add(offset)
        self.writes += 1
        self._record("write", offset, value)
        return True

    def facts(self):
        return dict(kind="selected-calibration-analysis/v1", analysis_only=True,
                    reason=self.reason, silicon_verified=False, analog_modelled=False,
                    boot_verified=False, irq_routed=False, completion_fabricated=self.backend is not None,
                    completion_policy="synchronous-on-rising-trigger-with-explicit-backend-and-setup-writes",
                    stop_policy="cancel-pending-retain-completed-results-until-retrigger-or-reset",
                    prerequisites_policy="write-presence-only-not-analog-configuration-validation",
                    invalid_status_data=0, ready=self.ready, pending=self.pending,
                    triggers=self.triggers, completions=self.completions,
                    reads=self.reads, writes=self.writes, result_reads=deepcopy(self.result_reads),
                    last_unsupported=deepcopy(self.last_unsupported), recent=deepcopy(self.recent))
