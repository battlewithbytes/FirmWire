"""Opt-in, ROM-bound instruction observations through compiled PANDA accessors."""


def validate_v0_trace(config):
    if not isinstance(config, dict) or not 1 <= len(config) <= 32:
        raise ValueError("v0_trace requires 1..32 named instruction addresses")
    points = {}
    for name, pc in config.items():
        if (not isinstance(name, str) or not name or len(name) > 64 or
                type(pc) is not int or pc < 0 or pc >= 2**32 or pc % 2):
            raise ValueError("v0_trace requires named, even 32-bit instruction addresses")
        if pc in points:
            raise ValueError("duplicate v0_trace instruction address")
        points[pc] = name
    return points


def install_v0_trace(panda, config, report, context_labels, persist):
    """Caller must first validate the enclosing RAM observer's ROM identity.

    Records v0 BEFORE selected instructions. This is not a direct CP0 read;
    only a validated firmware mfc0 immediately before a point establishes that
    relationship. No CPU structure dereferencing through Python/CFFI.
    """
    points = validate_v0_trace(config)
    if not hasattr(panda.libpanda, "panda_get_retval_external"):
        panda.ffi.cdef("target_ulong panda_get_retval_external(const CPUState *cpu);")
    read_v0 = panda.libpanda.panda_get_retval_external
    trace = {"read_only": True, "timing": "before instruction", "events": [],
             "total_events": 0, "counts": {}, "points": config,
             "accessor": "compiled panda_get_retval_external (MIPS v0)"}
    report["execution"]["v0_trace"] = trace

    @panda.cb_insn_translate
    def v0_translate(cpu, pc):
        return int(pc) in points

    @panda.cb_insn_exec
    def v0_execute(cpu, pc):
        pc = int(pc)
        if pc not in points:
            return 0
        label = context_labels.setdefault(cpu, "context-%d" % len(context_labels))
        trace["total_events"] += 1
        key = label + ":" + points[pc]
        trace["counts"][key] = trace["counts"].get(key, 0) + 1
        trace["events"].append({"pc": pc, "name": points[pc], "context": label,
                                "v0": int(read_v0(cpu)) & 0xffffffff,
                                "completed_blocks": report["execution"]["completed_blocks"]})
        del trace["events"][:-256]
        # Persist early arrivals; thereafter ordinary block/exception checkpoints
        # persist the same bounded ring. Avoid disk IO at every repeated check.
        if trace["counts"][key] <= 2:
            persist()
        return 0

    return trace
