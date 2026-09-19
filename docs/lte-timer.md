# LTE timer: RR configuration boundary

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

Next review the initializer's IRQ routing/configuration words at +0x4a4 onward,
then clock/trigger/expiry and interrupt-controller wiring independently.
Do not turn the entire register aperture into RAM or patch the post-fault PCCIF
notification 16 into an ordinary ring. No new mtkloader parser requirement has
been established by this register-behavior work.
