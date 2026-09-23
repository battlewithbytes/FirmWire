# Passive CCCI boot exchange observation

`hw/ccci_boot_observer.py` records the existing synthetic AP peer's handshake
when PCCIF0 control observation is explicitly enabled. It does not alter memory,
responses, interrupts, guest instructions or peripheral defaults. PCCIF1 is not
treated as the boot transport. No firmware hash, PC or physical address is in
the protocol observer.

ABI: `ccci-sram-query-ap-v2.1-ring-hs2/v1`. This names the existing transport's
supported layout, not all MediaTek devices. Other layouts require their own
reviewed decoder, not permissive matching in this one.

The observed sequence is:

1. SRAM HS1: 88-byte header/query prefix, control RX channel 0, command 0,
   reserved `0x5555ffff`, query head/tail `0x49434343`.
2. Existing synthetic response written: 172-byte AP v2.1 envelope, channel 1,
   declared size 172, reserved `0x5555ffff`, head/tail `0x43434349`.
3. Ring HS2: exactly a 16-byte control header, channel 0, command 0, reserved
   **different** from the HS1 check ID. No invented data0/sequence requirement.

This follows the HS1/HS2 distinction and startup ordering in the AP driver's
[`ccci_fsm_recv_control_packet` and startup FSM](https://github.com/MotorolaMobilityLLC/kernel-mtk/blob/android-13-release-ttt/drivers/misc/mediatek/eccci/fsm/ccci_fsm.c).
The SRAM query prefix and AP response layout match the preexisting transport.
The AP source is reference evidence, not proof that every modem uses this ABI.

The snapshot retains at most 32 metadata events and total event/violation counts;
it does not retain query feature payloads. Reordered, repeated, malformed or
unsupported boot messages permanently disqualify this observation session.
There is no automatic reset/recovery that could erase a prior violation.

`ordered_handshake_observed` means only the three transport events were observed
in order, without violations, with the **synthetic analysis peer**. Runtime
feature semantics, real AP communication, full modem readiness, timers, DSP,
RF operation and task progress are not established. All corresponding readiness
flags remain false. A legacy “finished booting” log has been replaced by a
neutral control-message receipt log.

Tests include negative framing/order/duplicate cases, bounded evidence and
serialization, PCCIF1 exclusion, and an actual PCCIF observer-on/off comparison
of the generated response memory and notification bits. The Cockpit saved-live
gate independently checks history/header fields, original image/DRDI readback,
profile and implementation hashes, and the narrow scope of the result.
