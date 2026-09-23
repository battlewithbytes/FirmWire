# Native execution observations

The native RAM/PC observer is opt-in, ROM-bound, read-only and bounded. Its
context labels identify opaque PANDA callback CPU pointers, not RTOS task IDs.
PC markers count normal translation-block exits whose **entry** address matches
the profile. They do not instrument arbitrary instructions in the middle of a
block or reconstruct a call stack.

## Early-exit correction

PANDA calls `after_block_exec` even when TCG has not started the block. In the
pinned engine, `tcg/tcg.h` defines exit codes 0/1 as normal exits and 2/3 as
instruction-count-expired / exit-requested attempts. `cpu-exec.c:226-249` calls
the callback before correcting the PC for those nonexecuted attempts.
Pandare disables TB chaining when registering a block callback.

`observation.normal_tb_exit` validates this engine contract. The observer now
counts/samples/records PC markers only for 0/1. `early_exit_callbacks` separately
counts rejected 2/3 callbacks, and `block_count_semantics: normal-tb-exit/v1`
qualifies the new evidence. Unknown codes fail instead of inventing execution.
The native IRQ regression exercises the same policy on both MIPS CPU models;
the actual observer method is also tested with deliberate 2/3 callback inputs.

Old captures remain valid historical artifacts, but their `completed_blocks`
and marker hits counted callback attempts, including nonexecuted blocks. Do not
silently reinterpret them as corrected counts. A service-progress acceptance
gate must reject unqualified legacy counts. The observation correction changes
no guest memory, instructions, hardware response, IRQ mask or scheduler state.

## Separate facts from conclusions

Even corrected marker counts are not full task/boot readiness. Require reviewed
target code to assign a role to a PC; keep entry/return observations context-local;
report incomplete or mismatched boundaries rather than relaxing the test.
Instruction-level `v0_trace` uses a compiled accessor and records before selected
instructions. Its bounded retained values and aggregate counts are different
evidence: a truncated history cannot prove every historical return value.

The MIPS hardware IRQ path bypasses PANDA's exception callback; see
`ccif-notifications.md`. Neither zero exception callbacks nor a growing block
count alone proves modem startup or a real AP handshake.

## Quiet scheduling observations

With a validated RAM observer and an explicitly realized MT topology, native
MTK checkpoints also capture `firmwire.scheduling-observation/v1` through the
reusable `emulator/scheduling_observation.py` helper. It retains the first and
latest 16 samples of virtual-clock nanoseconds and each requested CPU's index,
PC and QEMU/debugger `stopped` state. `stopped` is **not** architectural halt or
MIPS VPE/TC eligibility; false does not prove that CPU is being scheduled.

Sampling runs on the emulator thread at existing checkpoints, using compiled
`qemu_get_cpu`, `cpu_is_stopped`, `qemu_clock_get_ns` and `panda_current_pc`
accessors. CPU pointers remain opaque; no stale Python CPU-structure offsets
are dereferenced. Extra exported functions use ctypes because compiled CFFI
backends do not support adding `cdef` declarations at runtime. The existing
compiled CFFI PC accessor supplies the correct target word size.

There are no monitor requests, stop/resume commands, IRQ injections, clock
enable calls or guest writes. As with any instrumentation, timing overhead can
affect a race: successful instrumented runs do not establish a scheduling fix.
The API captures facts only and never sets a full-boot flag. Engines lacking
the required accessors or the requested CPUs fail explicitly.
