"""Bounded passive bus-access evidence; no register semantics or device reads."""
from collections import deque


class RegisterAccessObserver:
    def __init__(self, span, max_registers=1024, history=16):
        for value in (span, max_registers, history):
            if type(value) is not int or value <= 0:
                raise ValueError("observer limits must be positive integers")
        self.span = span
        self.max_registers = max_registers
        self.history = history
        self.registers = {}
        self.first = []
        self.recent = deque(maxlen=history)
        self.events = self.rejected = self.untracked = 0

    def record(self, operation, offset, size, value):
        if (operation not in ("read", "write") or type(offset) is not int
                or type(size) is not int or type(value) is not int
                or offset < 0 or size not in (1, 2, 4, 8)
                or offset + size > self.span or not 0 <= value < 1 << (8 * size)):
            self.rejected += 1
            return
        self.events += 1
        event = dict(sequence=self.events, operation=operation, offset=offset,
                     size=size, value=value)
        if len(self.first) < self.history:
            self.first.append(event)
        self.recent.append(event)
        key = (offset, size)
        if key not in self.registers:
            if len(self.registers) >= self.max_registers:
                self.untracked += 1
                return
            self.registers[key] = dict(offset=offset, size=size, reads=0, writes=0)
        entry = self.registers[key]
        entry[operation + "s"] += 1
        entry["last_" + operation] = value
        entry["last_" + operation + "_sequence"] = self.events

    def snapshot(self):
        return dict(schema="firmwire.register-access-observer/v1", read_only=True,
                    semantics_verified=False, events=self.events, rejected=self.rejected,
                    untracked=self.untracked, span=self.span,
                    registers=[dict(self.registers[key]) for key in sorted(self.registers)],
                    first=[dict(event) for event in self.first],
                    recent=[dict(event) for event in self.recent])
