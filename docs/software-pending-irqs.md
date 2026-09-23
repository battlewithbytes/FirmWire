# Selected software-pending IRQs

This is an opt-in **analysis** model, not full interrupt-controller emulation or
a declaration of modem readiness. Existing v1 CCIF profiles select no software
sources. The v2 profile adds a bounded explicit `software_sources` list; source
IDs and CPU wiring remain reviewed profile data, not modem names or PC hooks.

## Reusable layers

- `hw/irq_inputs.py`: `IRQInputLatch` owns external levels and software-pending
  bits separately. Their OR feeds the controller. Clearing one owner cannot
  clear the other. The component knows no register offsets, CPU architecture,
  firmware image or interrupt source identity.
- `hw/routed_irq.py`: validates and applies a batch of level changes before
  recomputing outputs. Packed register replacement cannot produce transient
  CPU pulses between individual bit updates.
- `vendor/mtk/hw/mdcirq_delivery.py`: the reviewed legacy register adapter
  implements selected direct (`+0x80`), CLEAR (`+0x120`) and SET (`+0x140`) bits.
  Unselected bits retain passthrough storage and are not advertised as modeled.
  Direct reads merge only the selected pending bits into that storage.
- The existing CPU endpoint/deferred handoff drives IRQ lines from emulator
  callbacks. A new asynchronous host event while **all CPUs are halted** is
  still not covered by this handoff; this work does not claim to solve that.

SET latches pending while masked; repeated SET coalesces. Claiming does not
clear the latch. Guest CLEAR removes the software request but does not unwind
the active interrupt stack. Guest return remains responsible for completion;
a request posted after CLEAR while the source is active survives return.

## Deliberate limits

Before SET can assert a selected software input, the guest must have disabled
GCR delivery and explicitly configured that source as normal, level-sensitive.
For these selected software bits the adapter interprets raw sensitivity bit
one as level, CLEAR (`+0x160`) as edge and SET (`+0x180`) as level. The v1
external-source sensitivity shadow is left unchanged; do not infer complete
raw-bank readback semantics from it.

Broadcast sources are supported only when the group has zero or one eligible
output. That is a degenerate, non-fan-out case; a pending source's route/group
cannot be changed to multiple eligible outputs. True broadcast, edge, NMI and
GCR delivery remain unsupported. Nonzero per-VPE IRQ/NMI mask accesses at
`+0x1b0/+0x1b4/+0x1b8` fail closed in the software-enabled experiment pending a
separate reviewed model. These controls were absent from the baseline capture.

## Evidence and observability

The initial Cockpit experiment selects only DCM sources 174/175. Its saved
dispatch table identifies `DCM_0`/`DCM_1`; target handlers `0x901cea98` and
`0x901ceaa4` call the existing decrement-and-clear routine `0x901d4d20`.
Older MOLY `dcm_service.c:588-610,754-767` corroborates acknowledgment and level
configuration; it is supporting evidence, not a substitute for target review.
OS notification sources 143–146 remain outside this first experiment.

Snapshots retain per-source claim/return totals, pending bits, post/clear counts
and the latest 32 selected software-register operations. Initial direct-bank
zero writes count as clears, so clear counts alone never prove serviced work.
The saved-live gate additionally requires a nonzero post, nonzero guest claim,
matching returns, and cleared pending state for explicitly required sources.
This is a narrow delivery milestone, not proof of task scheduling or full boot.

Unit tests exercise multiple source banks, masks, repeated posts, clear while
active, repost, ownership separation, packed-update atomicity, partial readback,
unsupported modes and single-destination broadcast across four output slots.
Native tests execute guest SET/claim/CLEAR/return/ERET on two MIPS CPU models.
No original modem bytes are modified by the implementation.
