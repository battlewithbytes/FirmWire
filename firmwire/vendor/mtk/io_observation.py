"""Bounded unassigned-MMIO metadata; never handles accesses or records payloads."""
import copy


class UnassignedIoTrace:
    LIMIT = 32

    def __init__(self):
        self.events = 0
        self.counts = {"read": 0, "write": 0}
        self.first = []
        self.recent = []

    def record(self, pc, address, size, write, context, blocks):
        self.events += 1
        direction = "write" if write else "read"
        self.counts[direction] += 1
        event = {"sequence": self.events, "pc": int(pc), "physical_address": int(address),
                 "size": int(size), "direction": direction, "context": context,
                 "completed_blocks": blocks}
        if len(self.first) < self.LIMIT:
            self.first.append(event)
        self.recent.append(event)
        del self.recent[:-self.LIMIT]

    def preceding(self, context, blocks):
        # Temporal/context association only. Another callback may have handled
        # these accesses. Do not call them proven faults or assert PC precision.
        return copy.deepcopy([e for e in self.recent
            if e["context"] == context and e["completed_blocks"] <= blocks][-8:])

    def snapshot(self):
        return copy.deepcopy({"read_only": True, "payloads_recorded": False,
            "handles_accesses": False, "other_callbacks_may_handle_accesses": True,
            "address_source": "PANDA unassigned-IO physical address",
            "pc_source": "PANDA callback PC; instruction precision not guaranteed",
            "events": self.events, "counts": self.counts, "first": self.first, "recent": self.recent})


def install_unassigned_io_observer(panda, report, context_labels):
    trace = UnassignedIoTrace()
    report["execution"]["unassigned_io"] = trace.snapshot()

    def record(cpu, pc, address, size, write):
        label = context_labels.setdefault(cpu, "context-%d" % len(context_labels))
        trace.record(pc, address, size, write, label, report["execution"]["completed_blocks"])

    @panda.cb_unassigned_io_read
    def observe_read(cpu, pc, address, size, value_pointer):
        record(cpu, pc, address, size, False)
        # Never dereference the output pointer or supply a value.
        return False

    @panda.cb_unassigned_io_write
    def observe_write(cpu, pc, address, size, value):
        record(cpu, pc, address, size, True)
        # The supplied payload is deliberately not retained.
        return False

    return trace
