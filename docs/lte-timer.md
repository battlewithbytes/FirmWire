# LTE timer: configuration boundary

`--mtk-loader-lte_timer 93xx-rr-config` explicitly selects a native-analysis
subset. Default is `disabled`. This is **not a working timer**.

The relocatable family peripheral uses the generic `ConfigurationRegisters`
storage primitive: explicit word offsets, arbitrary guest-provided values,
independent instances, snapshot-safe state, bounded counters. Unknown reset
values remain unknown; read-before-write fails instead of returning zero.
No ROM digest, PC, expected initializer values, or runtime table patches drive
the implementation. The loader explicitly maps the reviewed block at
0xa6090000, size 0x2000; unreviewed offsets in that window still fail.

## Evidence

Exact Lagos ROM SHA256:
`d915da6c23227b38107bb89340972ad84848a195e26cb0727b733351b6468582`.
Ghidra confirms a setter at 0x9032295c and reader at 0x90322acc: index <2,
stride 0x14, four words at +0x5c/+0x60/+0x64/+0x68 for bank 0 and
+0x70/+0x74/+0x78/+0x7c for bank 1. A distinct routine at 0x90322928
writes `(argument << 2 & 0x3ffffc) | 2` to +0x58/+0x6c.

The local sibling MT6765 LR12A V222.P10 ELF names corresponding routines
`EL1D_TC_HW_Set_RR_Value` (0x9031fbf4), `EL1D_TC_HW_Dump_RR_Value`
(0x9031fd64), and `EL1D_TC_HW_Trigger_RR_Event` (0x9031fbc0).
Their register access shapes agree with the target; their addresses are NOT
used to locate functions in other firmware. This establishes a configuration
versus trigger boundary, not the meaning of every RR bit.

Clock-start in the target at 0x90321f60 writes +4/+8 then 1 to +0;
clock-stop at 0x90321f94 writes 2 to +0. Those operations, RR triggers,
IRQ controls, status, and remaining initializer tables are unsupported.
The model does not claim nonzero RR values imply a running clock.

## Verification and next work

Tests vary all stored words and placements, reject trigger/clock/status and
unreviewed configuration offsets, reject invalid widths/types, preserve unknown
reset state, check reset/pickle/instance isolation and default-off/native gates.
Live acceptance must observe all eight original writes with unchanged firmware
and DRDI readback. A later strict stop is progress only through configuration,
not a modem boot or interrupt milestone.

With the explicit control observer enabled, unsupported accesses log a bounded
`LTE RR unsupported metadata=` snapshot before raising. A forwarded-MMIO worker
failure may prevent the next periodic capability sample; that older sample must
not be presented as failure-time state. Logging uses only local device state,
never synchronous guest-memory reads from the blocked MMIO worker.

Next review the initializer's IRQ routing/configuration words at +0x4a4 onward,
then clock/trigger/expiry and interrupt-controller wiring independently.
Do not turn the entire register aperture into RAM or patch the post-fault PCCIF
notification 16 into an ordinary ring. No new mtkloader parser requirement has
been established by this register-behavior work.

## Optional control configuration subset

`--mtk-loader-lte_timer 93xx-control` extends the RR subset with eight source
bitmaps at +0x4a4..+0x4c0, mode-bit storage at +0x4a0, and sixteen group-offset
words at +0x1b58..+0x1b94. It remains default-off and native-only. All values come
from guest writes. Unwritten words have unknown reset values; reads fail.
The older `93xx-rr-config` selection retains its original strict boundary.

The sibling V110.6 `libel1d.a:ltchwctrl.obj` initializer matches the target at
ROM offset 0x3229ec under relocation-aware comparison. Its named mask helper
writes zero to disable one output; unmask restores the firmware-computed
source bitmap. Mode helpers read/modify/write +0x4a0. The named helper bodies
themselves did **not** match the target: these are sibling behavioral evidence,
not exact-target symbol matches or universal-MTK register claims. Mode bits'
edge/level meaning is unverified and not interpreted by this implementation.

Lagos builds the eight masks in RAM from its own sixteen mapping bytes before
writing the registers. No table is injected or synthesized. Group parameters
are copied by the initializer from its own ROM. Group command registers start
at +0x1b98 and are excluded. Status words +0x4c4..+0x4e0 and writes +0x4ec/+0x4f0
remain unsupported: seeing a write of 0x3f does not establish storage, W1C, or
strobe semantics. Clock, trigger, expiry and actual interrupt routing are also
still unsupported. This is a configuration milestone, not a running timer.

Control failure snapshots use `LTE control unsupported metadata=`. Tests cover
all 33 offsets, varied values/bases, unknown-reset reads, command exclusions,
snapshot/reset isolation, and native guest source-mask program/clear/restore.
The saved-live gate additionally derives the expected masks from a hash-verified
target ROM; those exact-image constants are test oracles, never device defaults.

## Provisional initialization experiment (not a verified hardware contract)

`--mtk-loader-lte_timer 93xx-init-storage-analysis` explicitly opts into H0:
the two words +0x4ec/+0x4f0 retain independent 32-bit writes without timer or
IRQ side effects. Readback is the last supplied word; reset values stay unknown.
This extends the existing sparse configuration primitive, not the entire MMIO
window. It accepts arbitrary words, not just a particular image's initialization
constant. Other unknown registers and access widths remain rejected.

The profile is native-only, default-off, and does not alter `93xx-control`.
Capabilities and snapshots include `analysis_only: true`, the versioned
hypothesis and explicit assumptions, with `semantics_verified: false` and
`boot_verified: false`. Startup logs carry `LTE PROVISIONAL ANALYSIS`; the
first 32 accepted hypothesis accesses are logged, and the latest 32 retained
with bounded read/write counters. The failure snapshot uses
`LTE analysis unsupported metadata=`. No guest-memory reads or IRQ injection
occur from the MMIO worker.

Tests distinguish the provisional profile from both strict profiles, exercise
arbitrary values, relocated independent instances, bad accesses, bounded
observations and native guest write/readback. They validate the implemented
hypothesis, not real-silicon semantics. A write-only live run cannot distinguish
retained storage from other no-immediate-effect behaviors. Progress past these
writes is not evidence that timer expiry, IRQ routing or modem boot works.
