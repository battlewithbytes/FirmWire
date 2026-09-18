# IDC counter subset and interrupt-routing boundary

`--mtk-loader-idc_control mt6768-counter` opts into a deliberately narrow,
relocatable `MTKIDCControlPeripheral`. The MTK loader supplies physical base
`0xa60a0000`; the model itself contains no firmware PC, digest or base address.
The default is disabled, independently of `idc_uart`.

Only aligned 32-bit accesses with reviewed meaning are accepted:

- Write `1` at offset `0x0c`: enable TX counting.
- Read offset `0x10`: zero events transmitted by this idle model.
- Every other command/register: explicit unsupported-operation failure.

This is not a scheduler, fake peer, or successful-transmission generator. It
does not implement reset commands inferred from zero writes, event programming,
interrupt delivery, or count units. State facts expose those limitations and
the last unsupported access without recording payloads.

Evidence: current Lagos ROM Ghidra decode at `0x901d2df6` writes 1 to this
register; older MT6768 `drv_idc.c:392-393` calls it "Enable TX Count". Its
consumer reads the low 16 bits at offset `0x10`. The older source explains
intent, not universal compatibility. Tests exercise independent relocated
banks, malformed accesses, state serialization/reset and native MIPS MMIO.

## Remaining interrupt work

The native MIPS IRQ endpoint is tested separately; it is not yet connected to
this modem's MDCIRQ. Current-ROM decode confirms IDC UART source 83 and PM
source 82, level-sensitivity setup, and mask-set/clear accesses through the
MDCIRQ banks at `0xa0070000 + 0x60/+0x40` (word selected by source / 32).
Mask status uses `+0x20`.

Still establish from the current image: priority/group setup, group-to-VPE
routing, claim/read-ID/acknowledgment ordering, and vectored exception behavior.
Do not wire UART directly to a CPU input to bypass controller masks. Shared
CPU pins require aggregation, and the native endpoint must run on the emulator
thread, not the Avatar MMIO worker. Source counts, groups and VPE topology
belong in a reviewed hardware profile, not a Lagos-specific device class.
