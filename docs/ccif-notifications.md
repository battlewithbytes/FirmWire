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
notification remains independent. No CPU IRQ is connected, no guest firmware
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
counters and explicitly false CPU-interrupt/application-delivery flags.

## Normal IRQ adapter: tested but not installed in a modem

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

The actual modem continues to use passthrough MDCIRQ. A captured Lagos LISR
table leaves the older sibling source's candidate CCIF IDs 76/77 pointing at
the fatal default handler. They must not be wired based on the old header.
Establish the target's registration, source eligibility and output route before
connecting doorbell -> controller -> CPU. Notification, ACK and queue-consumer
progress must be measured independently.
