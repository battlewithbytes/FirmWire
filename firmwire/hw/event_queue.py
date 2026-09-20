"""Bounded event state independent of MMIO layout, CPU, clock and IRQ routing.

Deadlines are opaque nonnegative ticks supplied by the caller. This core never
reads host time, injects an interrupt, or infers a hardware timebase. A future
clock adapter can call take_due; a cancellation removes the queued event so
it cannot be delivered later. Event identities are (bank, bit). Callers must
serialize queue operations; this core does not own a worker or clock thread.
"""


class MaskedEventQueue:
    def __init__(self, banks, width=32):
        if (type(banks) is not int or not 1 <= banks <= 64 or
                type(width) is not int or not 1 <= width <= 64):
            raise ValueError("event queue requires 1..64 banks and bits")
        self.banks, self.width = banks, width
        self.reset()

    def reset(self):
        self._events = {}
        self.cancel_writes = self.cancelled_events = 0

    def _validate(self, bank, mask):
        if (type(bank) is not int or not 0 <= bank < self.banks or
                type(mask) is not int or not 0 <= mask < 1 << self.width):
            raise ValueError("event bank/mask outside configured width")

    def schedule(self, bank, mask, deadline):
        self._validate(bank, mask)
        if type(deadline) is not int or not 0 <= deadline < 2**64:
            raise ValueError("deadline must be an unsigned 64-bit tick")
        for bit in range(self.width):
            if mask & (1 << bit):
                self._events[bank, bit] = deadline

    def cancel(self, bank, mask):
        self._validate(bank, mask)
        removed = 0
        for bit in range(self.width):
            if mask & (1 << bit) and (bank, bit) in self._events:
                del self._events[bank, bit]
                removed |= 1 << bit
        self.cancel_writes = min(self.cancel_writes + 1, 2**64 - 1)
        self.cancelled_events = min(self.cancelled_events + removed.bit_count(), 2**64 - 1)
        return removed

    def take_due(self, now):
        if type(now) is not int or not 0 <= now < 2**64:
            raise ValueError("time must be an unsigned 64-bit tick")
        due = [(deadline, bank, bit) for (bank, bit), deadline in self._events.items() if deadline <= now]
        due.sort()
        for _, bank, bit in due:
            del self._events[bank, bit]
        return [dict(bank=bank, bit=bit, deadline=deadline) for deadline, bank, bit in due]

    def facts(self):
        return dict(banks=self.banks, width=self.width,
                    queued=[dict(bank=bank, bit=bit, deadline=deadline)
                            for (bank, bit), deadline in sorted(self._events.items())],
                    cancel_writes=self.cancel_writes, cancelled_events=self.cancelled_events)
