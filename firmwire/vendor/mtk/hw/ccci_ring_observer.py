"""Bounded transport evidence, not CCCI application or interrupt emulation."""


class RingObserver:
    def __init__(self, parent, rings):
        self.parent = parent
        self.rings = dict(rings)
        self.events = []
        self.queued_frames = {offset: 0 for offset in self.rings}
        self.guest_read_writes = {offset: 0 for offset in self.rings}

    def state(self, offset):
        if offset < 0 or offset + 24 > len(self.parent.mem):
            return {"valid": False, "reason": "control-out-of-bounds"}
        values = [self.parent.read_raw(offset + i * 4, 4) for i in range(6)]
        rr, rw, rc, tr, tw, tc = values
        valid = (rc >= 16 and tc >= 16 and rc % 8 == tc % 8 == 0
                 and offset + 24 + rc + tc <= len(self.parent.mem)
                 and all(0 <= p < c and p % 8 == 0
                         for p, c in ((rr, rc), (rw, rc), (tr, tc), (tw, tc))))
        return {"valid": valid, "md_to_ap": {"read": rr, "write": rw, "capacity": rc},
                "ap_to_md": {"read": tr, "write": tw, "capacity": tc,
                             "pending_bytes": (tw - tr) % tc if valid else None}}

    def record(self, event):
        self.events.append(event)
        del self.events[:-16]

    def before_guest_write(self, address, size):
        return {offset: self.state(offset) for offset in self.rings
                if address < offset + 16 and address + size > offset + 12}

    def after_guest_write(self, before, address, size):
        for offset, old in before.items():
            new = self.state(offset)
            self.guest_read_writes[offset] += 1
            delta = None
            if (address == offset + 12 and size == 4 and old["valid"] and new["valid"]
                    and old["md_to_ap"] == new["md_to_ap"]
                    and old["ap_to_md"]["capacity"] == new["ap_to_md"]["capacity"]
                    and old["ap_to_md"]["write"] == new["ap_to_md"]["write"]):
                previous, current = old["ap_to_md"], new["ap_to_md"]
                advance = (current["read"] - previous["read"]) % current["capacity"]
                if advance <= previous["pending_bytes"]:
                    delta = advance
            self.record({"kind": "guest-read-cursor-write", "offset": offset,
                         "before": old, "after": new, "bounded_advance_bytes": delta})

    def queued(self, offset):
        if offset in self.rings:
            self.queued_frames[offset] += 1
            self.record({"kind": "emulator-frame-queued", "offset": offset,
                         "state": self.state(offset)})

    def facts(self):
        return {"schema": "firmwire.ccci-ring-observation/v1", "read_only": True,
                "application_verified": False, "interrupt_delivery_verified": False,
                "cursor_semantics": "modulo positions; reset/full cycles cannot be distinguished",
                "rings": [dict(self.state(offset), offset=offset, label=label,
                               queued_frames=self.queued_frames[offset],
                               guest_read_writes=self.guest_read_writes[offset])
                          for offset, label in self.rings.items()],
                "recent_events": list(self.events)}
