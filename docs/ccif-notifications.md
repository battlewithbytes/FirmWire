# CCIF notifications and the normal IRQ bank

## Separate queueing, notification and interrupt delivery

`Ringbuf.writePacket` accepts an optional `on_reply` callback. It runs only after
the original response bytes, footer and producer cursor have been committed.
It does not change the consumer cursor. A rejected/full write does not notify.

`firmwire.hw.channel_doorbell.ChannelDoorbell` is a reusable 1..32-channel
pending-bit device, with level-change callback, coalescing counters and W1C
acknowledgement. It knows nothing about MediaTek, queue layouts, packet contents,
CPU models, modem images or IRQ numbers. A notification can remain pending after
its queue has drained, and an acknowledgement does not itself consume a queue.

The opt-in MTK loader option is:

```text
--mtk-loader-ccif_notifications ring-index-channel-bit-analysis
```

Default is `disabled`. This analysis ABI requires native mode and exactly one
PCCIF ring transport (`pccifid=0`). It uses ring index as the channel bit in
RCHNUM (`+0x10`); ACK (`+0x14`) clears selected bits. The existing SRAM/AMMS
notification remains independent. This option alone connects no CPU IRQ; no guest firmware
is patched, and no application or AP-peer delivery is claimed. Unknown ABIs,
ambiguous transports and rehosted selection are rejected before mapping.

This is explicitly selected, not a universal MediaTek convention. The reviewed
[AP CCIF driver](https://github.com/MotorolaMobilityLLC/kernel-mtk/blob/android-13-release-ttt/drivers/misc/mediatek/eccci/hif/ccci_hif_ccif.c)
commits the ring write before transmitting its queue's CCIF channel. Its
[register header](https://github.com/MotorolaMobilityLLC/kernel-mtk/blob/android-13-release-ttt/drivers/misc/mediatek/eccci/hif/ccif_hif_reg.h)
defines RINGQ_BASE=0, queue channels 0..7 and SRAM channel 15. Other revisions
can use other bases; add a reviewed adapter/ABI, not an image-name conditional
inside `ChannelDoorbell`. Exception transport and IRQ masks are not modeled by
this option.

PCCIF's optional `enable_control_observer()` records only completed accesses
below `+0x100`, excluding SRAM payloads. Its snapshot includes bounded channel
counters, actual CPU connection state and a separate false application-delivery flag.

## Normal IRQ adapter and explicit connection

The default remains unconnected. An explicit experimental connection is now
available as described below; the register adapter is still not a complete
MediaTek interrupt controller.

`firmwire.vendor.mtk.hw.mdcirq.MdcirqNormalIRQBank` adapts a reviewed subset of
the normal MDCIRQ page onto `RoutedLevelIRQController`. Construction requires
output sinks and an explicit `minimum_inclusive` choice. Priority-threshold
equality is still an analysis policy, not established silicon behavior.

Source count and offsets are configurable. Layout validation rejects overlapping
banks: the default offsets work for 1/2/4 outputs; an eight-output test uses an
explicit nonoverlapping layout. CPU topology is not hardcoded to two VPEs.
Supported operations are mask/read/set/clear, packed priorities/groups,
inverse-mask group routes, per-output threshold/state, ID claim, current
ID/priority, idle stack initialization and nested return. Packed changes publish
only final output levels to the CPU.

The return register restores the **previous** source ID. The adapter validates
that value and completes the current claimed source in the generic core. It
does not pass the previous ID to the generic completion API. NMI, edge,
broadcast, GCR, software-trigger and active priority-ACK semantics are explicitly
unsupported; this is not a drop-in replacement for the full controller.

`FIRMWIRE_TEST_NATIVE_IRQ=1` runs real guest ISR/claim/device-clear/return/ERET
tests on both `24Kc` and `cockpit-mtk-legacy`. This proves the implemented
synthetic contract and native CPU line path, not a firmware IRQ mapping.

By default the modem uses passthrough MDCIRQ. The finalized Lagos LISR
captures independently confirm IDs 76/77 as `pccif0irq0`/`pccif0irq1` in both
baseline and notification runs. An earlier interpretation that they remained
at the fatal default handler was incorrect for the completed runs. Registration
does not establish source eligibility: the observed packed priority for 76..79
remains `0x7f7f7f7f`. The selected-source experiment below makes its reviewed
threshold policy and output routing explicit. Notification, ACK and
queue-consumer progress must be measured independently.

## Opt-in selected-source delivery

`--mtk-loader-ccif_irq_profile PATH` selects a strictly validated
`firmwire.ccif-normal-irq-analysis/v1` JSON profile. It requires native mode,
realized explicit CPU topology, enabled ring notifications, matching ROM/CPU
identity, named typed controller/transport devices, disjoint channel masks and
explicit source/output wiring. It is never auto-enabled for unknown firmware.

The profile contains `schema`, `rom_sha256`, `cpu_model`, nonempty `evidence`,
`controller`, `transport`, explicit boolean `minimum_inclusive`, `outputs`
(`cpu_index`, `pin`), and `routes` (`channel_mask`, `source`). Outputs must match
the realized CPU count without duplicates; legacy bank overlap validation
still rejects unsupported geometry. No source number, CPU count, firmware PC
or image name is embedded in the generic devices.

`ChannelIRQRouter` maps disjoint channel masks to persistent levels. The new
`MdcirqLevelDelivery` connects only selected external normal, dynamic, level
inputs; selected edge/broadcast/NMI/software-trigger modes fail explicitly.
Guest mask aliases, priorities, groups, routes and claim/previous-ID return
control delivery. Selected sources start masked; no forced unmask or register
patches are applied. The idle current-priority read includes bit 7 (minimum
priority discriminator); active IRQ priority does not.

Shared priority/group words preserve **all original bytes for readback**.
Unselected fields (including NMI-only groups 16/17) are stored, not interpreted
by the normal-IRQ core. All unselected interrupt inputs remain unmodeled, and
other registers retain existing passthrough behavior. Capability facts state
these limits explicitly. This subset is not NMI/GCR/edge/broadcast/timer emulation.

Avatar may invoke an MMIO device on its dispatch thread. `DeferredIRQOutputs`
therefore transfers desired levels to a PANDA after-block callback: only that
emulator-thread callback calls `MipsIRQInput`. Unit tests verify thread ownership;
native tests cover notify -> claim -> ACK -> previous-ID return -> ERET on both
MIPS models through the deferred path. Pulses are not this handoff's contract.

The reusable controller's `configure_many` validates a whole update before
publishing it. Packed priority/group changes and route changes produce no
intermediate native line edges and no quadratic per-source recomputation.

The initial Lagos profile selects **only source 76** for normal channel bits.
Target Ghidra shows its handler masks the source and activates worker `0x7c`.
Source 77 is a different, broadcast group and handles high event bits; it is
not connected. The actual packed group word is `0x04040504`, meaning group 4
for source 76 and group 5 for 77, not group 4 for both. Idle equality is supported
by the sibling driver's explicit comparison (not a claim of silicon fidelity).

Snapshot `irq_delivery` separates asserted input, guest mask, priority, group,
route, claims, returns and active stacks. `irq_output_handoff` shows desired and
driven CPU levels. Connection/configuration facts stay separate from successful
guest service, task progress, real AP communication and full boot.

The unchanged Lagos 900-second connected experiment completes 96 source-76
claims and previous-ID returns with 192 CPU-line transitions. Its previously
pending SWTP/filesystem reply queues drain. Other sources remain unmodeled;
full boot and AP communication remain unverified. Firmware configuration is
not forced to unmask the source. Synthetic and native regressions pass:
394 tests, three skipped in the full selected suite.

PANDA's MIPS hardware interrupt path bypasses `before_handle_exception`;
empty callback counts are not evidence of absent IRQs. Validate claims/returns,
line changes and queues independently. The after-block level handoff also does
not prove externally initiated wakeup when all CPUs are halted; a future real
AP bridge needs a tested emulator-thread wakeup mechanism for that case.
