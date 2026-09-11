"""Bounded address-only exception evidence; never suppresses guest exceptions."""
import copy


class ExceptionTrace:
    LIMIT = 32

    def __init__(self):
        self.events = 0
        self.counts = {}
        self.first = []
        self.recent = []

    def record(self, index, pc, context, completed_blocks, recent_pcs):
        self.events += 1
        key = str(index) if type(index) is int and 0 <= index < 64 else "other"
        self.counts[key] = self.counts.get(key, 0) + 1
        event = {"exception_index": index, "pc": pc, "context": context,
                 "completed_blocks": completed_blocks, "preceding_pcs": list(recent_pcs[-16:])}
        if len(self.first) < self.LIMIT:
            self.first.append(event)
        self.recent.append(event)
        del self.recent[:-self.LIMIT]
        return index  # PANDA's callback return can replace an exception. Never do so.

    def snapshot(self):
        return copy.deepcopy({"read_only": True, "events": self.events, "counts": self.counts,
                              "first": self.first, "recent": self.recent,
                              "registers_or_payloads_recorded": False})


def install_exception_observer(panda, report, context_labels, persist):
    """Use the compiled PC accessor, not stale Python CPUArchState layouts."""
    trace = ExceptionTrace()
    report["cpu_exception_observer"] = {"read_only": True,
        "pc_source": "native panda_current_pc", "exception_indices": "engine-specific, not architectural Cause codes"}
    report["execution"]["cpu_exceptions"] = trace.snapshot()

    @panda.cb_before_handle_exception
    def before_exception(cpu, index):
        execution = report["execution"]
        label = context_labels.setdefault(cpu, "context-%d" % len(context_labels))
        context = execution["per_context"].get(label, {})
        pc = int(panda.libpanda.panda_current_pc(cpu))
        trace.record(index, pc, label, execution["completed_blocks"], context.get("recent_pcs", []))
        # Persist promptly even if a later MMIO dispatch thread dies. Bound
        # disk activity after the first events if an exception flood occurs.
        if trace.events <= trace.LIMIT or trace.events & (trace.events - 1) == 0:
            execution["cpu_exceptions"] = trace.snapshot()
            persist()
        return index

    return trace
