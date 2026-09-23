# Software-trigger bank review

The selected external-level delivery adapter does not implement software-pending
interrupts. It must reject a selected source written to the direct trigger bank
(`0x80`) or SET bank (`0x140`). CLEAR (`0x120`) must not clear an externally
asserted input. Unselected inputs retain the existing unmodeled passthrough.

The old `MDCPeripheral.py` comments reversed SET and CLEAR. The delivery guard
inherited that error. This correction does not add software IRQ delivery or
claim to unblock modem startup.

Evidence, without redistributing vendor source or firmware:

- Older MOLY LR12A `mcu/common/driver/devdrv/cirq/inc/drv_mdcirq_reg.h:324-340`
  defines CLEAR at `0x120` and SET at `0x140`.
- Its `src/drv_mdcirq.c:1698-1722` uses SET for activation and CLEAR for reset.
- The independently decoded Lagos ROM has the bit-write helper at `0x901d4c34`
  using `0xa0070140`, and the helper at `0x901d4c5c` using `0xa0070120`.
  The latter is called when the software-interrupt reference count falls to
  zero. These PCs are evidence for a reviewed family layout, never runtime
  dispatch conditions.
- Cockpit local Ghidra artifact:
  `runtime/ccci-boot-readiness/modem-ghidra-quax8dpr`, produced from the
  mtkloader-prepared original ROM (SHA256
  `d915da6c23227b38107bb89340972ad84848a195e26cb0727b733351b6468582`).

Tests cover selected sources in multiple words, unselected bits, zero writes,
rejection without controller mutation, and CLEAR preserving an external input.

## Separate sensitivity question

Do not infer the meaning of sensitivity bits from those legacy comments either.
The older source labels `0x160` CLEAR and `0x180` SET, and maps edge=true to
CLEAR. Lagos `0x901d4c0c` likewise sends argument one to `0x160` and other
values to `0x180`. The adapter currently uses an edge-mode shadow with the
opposite alias operations and refuses set shadow bits. This gives the intended
edge/level choice for those alias writes, but does not establish correct raw
bank readback/reset semantics. Audit direct-register semantics independently
before extending sensitivity or software-pending delivery. Nothing here proves
edge, NMI, GCR or broadcast interrupt support.
