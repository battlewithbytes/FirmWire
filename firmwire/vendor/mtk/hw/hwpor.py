"""Opt-in digital RF POR sequencer core; not connected to machine defaults.

Register fields follow the MT6768 reference header and decoded guest writers.
Scheduling uses explicit logical ticks, not wall time or guest polls. Trigger
strobes self-clear in this analysis model; silicon timing/arbitration is not
claimed. Only trigger 0, RF writes, and global-offset selector 0 are supported.
The caller supplies a backend and advances time. No ROM parser, RF identity,
calibration result, or firmware-specific table/offset lives in this module.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HwporLayout:
    registers: int
    data: int
    event_count: int = 16
    slot_count: int = 128

    def __post_init__(self):
        if (type(self.event_count) is not int or not 1 <= self.event_count <= 16
                or type(self.slot_count) is not int or not 1 <= self.slot_count <= 128
                or any(type(v) is not int or v < 0 or v % 4 for v in (self.registers, self.data))
                or max(self.registers, self.data) >= 2**32
                or self.data < self.registers + 16 + 8*self.event_count
                and self.registers < self.data + 8*self.slot_count
                or self.registers + 16 + 8*self.event_count > 2**32
                or self.data + 8*self.slot_count > 2**32):
            raise ValueError("Invalid or overlapping HWPOR layout")


@dataclass(frozen=True)
class HwporWrite:
    sequence: int
    event: int
    slot: int
    port: int
    word: int


class HwporSequencer:
    """submit(write) returns True ONLY after handling the write, False if pending.

    Pending writes can be acknowledged through complete(sequence), never by a
    poll. Unknown backend semantics must return False. No backend means pending.
    The sequencer does not own/reset a potentially shared serial target.
    """
    def __init__(self, layout, submit=None):
        if not isinstance(layout, HwporLayout) or submit is not None and not callable(submit):
            raise ValueError("HWPOR requires a reviewed layout and callable backend")
        self.layout, self.submit = layout, submit
        self.sequence = 0
        self.reset()

    def reset(self):
        self.storage = {}
        self.status = self.ticks = self.completed_writes = self.triggers = 0
        self.queue = []
        self.pending = None
        self.log = []
        # Sequence is not rewound: a late pre-reset completion must be rejected.

    def _access(self, offset, size):
        l = self.layout
        if (type(offset) is not int or size != 4 or offset % 4
                or not (l.registers <= offset < l.registers + 16 + 8*l.event_count
                        or l.data <= offset < l.data + 8*l.slot_count)):
            raise ValueError("HWPOR requires an aligned 32-bit modeled register")

    def read(self, offset, size=4):
        self._access(offset, size)
        if offset == self.layout.registers + 8:
            return self.status
        if offset == self.layout.registers + 12:
            return 0
        return self.storage.get(offset, 0)

    def write(self, offset, value, size=4):
        self._access(offset, size)
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("Invalid HWPOR register value")
        base = self.layout.registers
        if offset == base + 8:
            return  # read-only completion status
        if offset == base + 12:
            if value >> self.layout.event_count:
                raise ValueError("Unsupported event-clear bits")
            for event in range(self.layout.event_count):
                if value & (1 << event):
                    self.status &= ~(3 << (2*event))
            return
        if offset == base + 4:
            if value & ~7:
                raise NotImplementedError("Only HWPOR trigger 0 is reviewed")
            if value & 6 and (self.queue or self.pending):
                raise ValueError("HWPOR trigger/clear while busy is not modeled")
            if value & 2 and value & 4:
                raise ValueError("Simultaneous HWPOR force/clear is not modeled")
            if value & 3 == 3:
                self._trigger()  # validates every event before publishing state
            self.storage[offset] = value & 1  # explicit analysis strobe policy
            return
        if offset == base and value >> self.layout.event_count:
            raise ValueError("Unsupported event-enable bits")
        self.storage[offset] = value

    def _trigger(self):
        l = self.layout
        events = []
        for event in range(l.event_count):
            if not self.storage.get(l.registers, 0) & (1 << event):
                continue
            timing = self.storage.get(l.registers + 16 + 8*event, 0)
            config = self.storage.get(l.registers + 20 + 8*event, 0)
            if timing & ~0xfffff or config & ~0x007f007f:
                raise NotImplementedError("Unsupported HWPOR timing/global-offset/control bits")
            first, last = config & 127, (config >> 16) & 127
            if not first <= last < l.slot_count:
                raise ValueError("Invalid HWPOR slot span")
            slots = []
            for slot in range(first, last + 1):
                address = l.data + 8*slot
                if address not in self.storage or address + 4 not in self.storage:
                    raise ValueError("HWPOR slot was not programmed")
                word, port = self.storage[address], self.storage[address + 4]
                if port & ~15:
                    raise NotImplementedError("HWPOR MIPI/extended port configuration is not modeled")
                slots.append((slot, port, word))
            events.append((timing, event, slots))
        queue = []
        for timing, event, slots in sorted(events):
            for index, (slot, port, word) in enumerate(slots):
                queue.append((self.ticks + timing, event, slot, port, word, index == len(slots)-1))
        for _, event, _ in events:
            self.status &= ~(3 << (2*event))
        self.queue = queue
        self.triggers += 1

    def advance(self, ticks):
        if type(ticks) is not int or ticks < 0:
            raise ValueError("HWPOR time must advance by nonnegative logical ticks")
        self.ticks += ticks
        while self.queue and self.pending is None and self.queue[0][0] <= self.ticks:
            _, event, slot, port, word, _ = self.queue[0]
            self.sequence += 1
            self.pending = HwporWrite(self.sequence, event, slot, port, word)
            self.log.append(asdict(self.pending))
            del self.log[:-64]
            result = False if self.submit is None else self.submit(self.pending)
            if type(result) is not bool:
                raise ValueError("HWPOR backend must explicitly report handled or pending")
            if result:
                self.complete(self.pending.sequence)

    def complete(self, sequence):
        if type(sequence) is not int or self.pending is None or self.pending.sequence != sequence:
            raise ValueError("No matching pending HWPOR write")
        _, event, _, _, _, last = self.queue.pop(0)
        self.completed_writes += 1
        self.pending = None
        if last:
            self.status |= 3 << (2*event)

    def facts(self):
        return {"abi": "mt6768-rfpor-digital/v1", "analysis_only": True,
                "firmware_boot_verified": False, "silicon_verified": False,
                "timing": "explicit-logical-ticks-no-poll-side-effects",
                "trigger_policy": "self-clearing-strobes-no-active-cancel",
                "event_snapshot_policy": "at-trigger; equal-times-event-index-order",
                "layout": asdict(self.layout), "ticks": self.ticks, "status": self.status,
                "triggers": self.triggers, "completed_writes": self.completed_writes,
                "pending": asdict(self.pending) if self.pending else None,
                "queued_writes": len(self.queue), "recent_writes": list(self.log)}
