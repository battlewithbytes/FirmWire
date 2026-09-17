"""Bounded exception evidence with opt-in stack capture; never suppresses faults."""
import copy


class ExceptionStackCapture:
    """Opt-in, bounded stack evidence at reviewed exception sites.

    The caller must bind the configuration to the ROM and validate every
    requested word against ordinary RAM. SP comes from a compiled accessor,
    never Python CPU structure offsets. No pointer chasing or register writes.
    """
    def __init__(self, config, read_sp, validate_words, read_bytes):
        if (not isinstance(config, dict) or set(config) != {"pcs", "exception_index", "words"}
                or type(config["exception_index"]) is not int
                or not 0 <= config["exception_index"] < 64
                or type(config["words"]) is not int or not 1 <= config["words"] <= 16
                or not isinstance(config["pcs"], list) or not 1 <= len(config["pcs"]) <= 4
                or any(type(pc) is not int or not 0 <= pc <= 0xfffffffe or pc & 1
                       for pc in config["pcs"])
                or len(set(config["pcs"])) != len(config["pcs"])):
            raise ValueError("Invalid bounded exception-stack capture")
        self.config = copy.deepcopy(config)
        self.read_sp, self.validate_words, self.read_bytes = read_sp, validate_words, read_bytes
        self.attempts = 0

    def sample(self, cpu, index, pc):
        if (index != self.config["exception_index"] or pc not in self.config["pcs"]
                or self.attempts >= 4):
            return None
        self.attempts += 1
        try:
            sp = int(self.read_sp(cpu))
            words = [sp + offset * 4 for offset in range(self.config["words"])]
            self.validate_words(words)  # validate ALL words before any guest read
            raw = bytes(self.read_bytes(sp, len(words) * 4))
            if len(raw) != len(words) * 4:
                raise ValueError("Short stack read")
            return {"read_only": True, "sp": sp, "address_space": "validated-physical-RAM",
                    "words": [int.from_bytes(raw[i:i+4], "little") for i in range(0, len(raw), 4)]}
        except Exception as exc:
            # Failure to collect evidence must not replace/suppress the guest fault.
            return {"read_only": True, "error": type(exc).__name__ + ": " + str(exc)}


class ExceptionTrace:
    LIMIT = 32

    def __init__(self):
        self.events = 0
        self.counts = {}
        self.first = []
        self.recent = []
        self.stack_recorded = False

    def record(self, index, pc, context, completed_blocks, recent_pcs, stack=None, io_events=None):
        self.events += 1
        key = str(index) if type(index) is int and 0 <= index < 64 else "other"
        self.counts[key] = self.counts.get(key, 0) + 1
        event = {"exception_index": index, "pc": pc, "context": context,
                 "completed_blocks": completed_blocks, "preceding_pcs": list(recent_pcs[-16:])}
        if stack is not None:
            event["stack"] = copy.deepcopy(stack)
            self.stack_recorded |= "words" in stack
        if io_events is not None:
            event["preceding_unassigned_io"] = copy.deepcopy(io_events)
        if len(self.first) < self.LIMIT:
            self.first.append(event)
        self.recent.append(event)
        del self.recent[:-self.LIMIT]
        return index  # PANDA's callback return can replace an exception. Never do so.

    def snapshot(self):
        return copy.deepcopy({"read_only": True, "events": self.events, "counts": self.counts,
                              "first": self.first, "recent": self.recent,
                              "registers_or_payloads_recorded": self.stack_recorded})


def install_exception_observer(panda, report, context_labels, persist, stack_capture=None, io_trace=None):
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
        stack = stack_capture.sample(cpu, index, pc) if stack_capture is not None else None
        io_events = io_trace.preceding(label, execution["completed_blocks"]) if io_trace else None
        trace.record(index, pc, label, execution["completed_blocks"], context.get("recent_pcs", []), stack, io_events)
        # Persist promptly even if a later MMIO dispatch thread dies. Bound
        # disk activity after the first events if an exception flood occurs.
        if trace.events <= trace.LIMIT or trace.events & (trace.events - 1) == 0:
            execution["cpu_exceptions"] = trace.snapshot()
            if io_trace is not None:
                execution["unassigned_io"] = io_trace.snapshot()
            persist()
        return index

    return trace
