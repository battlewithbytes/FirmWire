# Extensible CCCI mailbox services

Disabled by default. Select `--mtk-loader-ccci_mailbox_profile path.json`
in **native** boot mode to configure reviewed AP-side analysis services.
This is a protocol endpoint, not another UART or RF peripheral.

```json
{
  "schema": "firmwire.ccci-mailbox-profile/v1",
  "profile": "legacy-md1-swtp-pin-out-analysis",
  "source": "Your reviewed ABI reference and explicit analysis assumption",
  "services": [{"kind": "legacy-md1-swtp-analysis/v1", "mode": 0}]
}
```

The initial profile uses an explicitly synthetic pin-out state (0); pin-in (1)
is also supported. Neither value is measured RF power, calibration data, or an
assertion about the target board. No implicit state default is supplied.
The profile is a reviewed ABI selection, not automatic support for arbitrary MTK
images. Unknown service kinds, missing/extra fields, duplicate routes, conflicts
with RPC/FS/control/IPC or unopened ports, and missing/ambiguous PCCIF0 bindings
are rejected before memory mappings are created.

## Responsibility split

- `hw/ccci_mailbox.py`: exact 16-byte, little-endian mailbox envelope with
  `data0 == 0xffffffff`; immutable typed messages/results; explicit registry
  keyed by `(channel, message_id)`. Reserved is an opaque parameter, never a
  pointer. No wildcard handler, automatic ACK, payload retention, or platform
  detection. Stream and extended mailbox variants require a separate decoder.
- `hw/ccci_swtp.py`: software TX-power query endpoint. Its ABI and state provider
  are constructor inputs. `TxPowerState` can be implemented by a future board
  model; `FixedTxPowerState` is the explicitly synthetic backend used here.
- `ccci_mailbox_profile.py`: strict JSON profile plus a service-factory registry.
  New service families get separate endpoint modules and reviewed factories;
  JSON selects supported behavior, not executable code or arbitrary responses.
- `PCCIFPeripheral.py`: existing ring transport. A handler returns a
  `MailboxResult` with an optional validated response. PCCIF queues that response
  on the originating ring and reports `response_queued`. No new interrupt,
  sequence-number policy, AP boot FSM, or transport retry mechanism is invented.

An accepted request is **not** proof the guest consumed the reply. Capability
facts keep `peer_connected`, `application_verified`, `guest_delivery_verified`,
and `boot_verified` false. Unrecognized IDs on a registered channel fail rather
than falling through to an unopened-port sink. Parameters are omitted from
logs and message repr. Mailbox command IDs are safe diagnostic metadata.

## Initial reviewed service

The explicitly selected legacy MD1 ABI receives channel 2 / command `0x110`
and replies on channel 3 / command `0x10e`, with the selected pin state in
reserved. Request parameters are ignored by the reference callback; auxiliary
bits are not copied into the reply. This sends a state update, not a generic
success code. The service does not model GPIO transitions, RF effects, boot
state gating, asynchronous updates, or retry workqueues.

Reference: MiCode/Xiaomi_Kernel_OpenSource revision
`8b776e0714fb1eb2e02daae4f730a2f17bf1e2ca`:

- [port_sysmsg.c](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/8b776e0714fb1eb2e02daae4f730a2f17bf1e2ca/drivers/misc/mediatek/eccci/port/port_sysmsg.c): request callback dispatch.
- [ccci_swtp.c](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/8b776e0714fb1eb2e02daae4f730a2f17bf1e2ca/drivers/misc/mediatek/eccci/port/ccci_swtp.c): pin-state query and kernel API call.
- [ccci_core.c](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/8b776e0714fb1eb2e02daae4f730a2f17bf1e2ca/drivers/misc/mediatek/eccci/ccci_core.c): `ID_UPDATE_TX_POWER` sends `MD_SW_MD1_TX_POWER` on `CCCI_SYSTEM_TX`.
- [mtk_ccci_common.h](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/8b776e0714fb1eb2e02daae4f730a2f17bf1e2ca/drivers/misc/mediatek/include/mt-plat/mtk_ccci_common.h): command constants.

These are sibling-source contracts, not proof of every target's ABI. The Lagos
diagnostic observed the 16-byte request channel/ID directly. No ROM offsets,
instruction PCs, final firmware tables, or boot-success overrides appear in
the implementation.

## Extending and testing

Add a `MailboxEndpoint` subclass for a genuinely new service, or inject a
different `SoftwareTxPowerABI`/`TxPowerState` for a reviewed SWTP variant. Add
its explicit factory and evidence reference; do not change PCCIF dispatch.
Test unrelated route IDs, both pin states, changing providers, unknown IDs,
malformed frames, duplicate/colliding registrations, and failed queueing.
Real-ring tests verify the exact outgoing frame through every aligned wrap
position and confirm no new IRQ state is asserted. Existing IPC/FS/RPC/control
paths remain separate.
