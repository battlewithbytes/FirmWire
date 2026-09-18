# Unopened AP ports and coalesced receive rings

`--mtk-loader-ccci_closed_ports <profile.json>` is native-only, default absent.
It represents explicitly selected AP character devices with **no open consumer**,
not an IMS/RIL service and not a connected AP. It is independent of the opt-in
WMT endpoint and can be supplied without enabling IPC.

Schema `firmwire.ccci-closed-ports/v1` requires `profile`, `source` and a bounded
`channels` list. Every entry supplies numeric `channel`, `name` and
`max_packet_bytes`. Duplicate channels, invalid bounds and collisions with the
existing control/FS/RPC/IPC handlers are rejected before peripheral mapping.
Profiles are explicit reviewed ABI/policy choices, not auto-selected hardware
facts. A size cap is an analysis limit unless independently derived.

`ClosedAPPorts` copies descriptors and retains only saturating per-channel
counters. Its constructor can describe other reviewed channel assignments
without changing emulator code. Unknown channels fail; configured channels
consume opaque packets and log bounded header/disposition metadata, never
payloads, application replies or IRQs. There is no universal drop policy.

Reference: the older [AP character-port driver](https://android.googlesource.com/kernel/mediatek/+/android-mediatek-sprout-3.10-marshmallow-mr2/drivers/misc/mediatek/eccci/port_char.c)
drops receives when usage count is zero, with a UART2 exception. The supplied
IMSA profile uses sibling MOLY's 0x39 channel and this unopened-port behavior;
it does not claim to implement every AP-driver version or its exceptions.

The observed ring contained 88 bytes when the first packet used only 40 framed
bytes. PCCIF now drains coalesced packets per doorbell, with a ring-capacity
work budget and an explicit error if exhausted. Empty doorbells are harmless.
Known RPC/FS/control handlers are unchanged; the dispatcher is extracted into
`dispatch_ccci_packet` so transport draining and service selection stay separate.

Receive parsing validates pointer/capacity bounds and alignment, wrapped header,
padded packet length against available bytes, and both footer words before
advancing the read pointer. A malformed ring does not partially advance it.
This is receive-side validation only; it does not redesign TX ring backpressure.

Tests exercise every aligned start in a synthetic ring with several packet
lengths, multi-packet wraps, mixed existing handlers, unknown channels, malformed
frames, finite work under continuous refill, instance/pickle isolation and no
response or IRQ writes. Live startup acceptance is a separate saved-artifact
gate; passing ring tests is not modem boot or real AP interoperability.
