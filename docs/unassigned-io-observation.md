# Unassigned I/O observation

The ROM-bound `observe_ram` JSON profile may opt into `unassigned_io: true`
alongside `cpu_exceptions: true`. It is disabled by default. Non-boolean values
or use without exception observation are rejected. The observer changes neither
the memory map nor peripheral behavior.

It records physical address, size, direction, callback PC, opaque context,
completed blocks and event sequence. It retains at most 32 first and 32 recent
events, plus two direction counters. It never reads the read-result pointer,
records payloads or writes guest state. Both callbacks return false, preserving
the engine's normal handling/fault behavior. Native tests exercise both callback
orders with another handler: successful I/O stays successful, and an unhandled
read/write still produces a data-bus exception.

An exception receives up to eight preceding same-context events. This is
correlation, not a declaration that every event faulted: another callback may
handle the access. Callback PCs may be unavailable/zero or imprecise, and the
exception accessor may identify a block rather than its faulting instruction.
Physical addresses are taken directly from PANDA; do not infer them using a
different machine's translation rules. Snapshot metadata states these limits.

Evidence persists with exceptions and ordinary periodic execution checkpoints,
not on every access. An exception flood still uses the existing bounded/exponential
persistence schedule. Default exception reports remain unchanged without opt-in.

Tests: `test_mtk_io_observation.py`, `test_mtk_checkpoint_adapter.py`, and
`FIRMWIRE_TEST_NATIVE_IO=1` for `test_mtk_io_native.py`. The native test is
firmware-free, bounded and uses synthetic data; no physical device is needed.

The first live use on unchanged Lagos located a 4-byte write to `0xa60b0024`
after RF calibration return. The corresponding missing IDC UART model is a
separate implementation task; observation alone does not advance boot.
