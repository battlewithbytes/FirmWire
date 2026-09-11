"""Bounded, opt-in host-side AES MMIO evidence. Never stores key/payload values."""
from copy import deepcopy


class AESControlTrace:
    # Security-initialization words are also redacted: their meaning may vary.
    CONTROL_OFFSETS = frozenset((0, 4, 8, 12))

    def __init__(self):
        self.counts = {}
        self.first_controls = []
        self.recent_controls = []
        self.events = 0

    def record(self, operation, offset, size, value):
        self.events += 1
        # Fixed buckets bound storage even for malformed/unaligned accesses.
        bucket = hex(offset) if type(offset) is int and offset % 4 == 0 and 0 <= offset <= 0x88 else "other"
        counts = self.counts.setdefault(bucket, {"read": 0, "write": 0})
        counts[operation] += 1
        if size != 4 or offset not in self.CONTROL_OFFSETS:
            return
        event = dict(sequence=self.events, operation=operation, offset=offset, value=value)
        if len(self.first_controls) < 32:
            self.first_controls.append(event)
        # Consecutive equal controls collapse; counts still record all accesses.
        if self.recent_controls:
            previous = self.recent_controls[-1]
            if all(previous[key] == event[key] for key in ("operation", "offset", "value")):
                previous["last_sequence"] = self.events
                return
        self.recent_controls.append(event)
        del self.recent_controls[:-32]

    def snapshot(self):
        return deepcopy(dict(schema="cockpit.mtk-aes-controls/v1", read_only=True,
            key_and_payload_values_recorded=False, events=self.events,
            access_counts=self.counts, first_controls=self.first_controls,
            recent_controls=self.recent_controls))
