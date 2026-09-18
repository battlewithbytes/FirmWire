# Explicit CCCI IPC analysis endpoints

`--mtk-loader-ccci_ipc wmt-unavailable` enables one native-only analysis policy.
Default `disabled` retains strict unknown-channel failure. This does not connect
FirmWire to Android or implement a ready Wi-Fi/Bluetooth connectivity processor.

`hw/ccci_ipc.py` separates:

- `decode_ilm32`: bounded channel-0x22 header plus six-word ILM, inline local
  parameter length checking, no guest-pointer dereference. Peer buffers remain
  unsupported. The reviewed WMT envelope is capped at 1024 parameter bytes.
- `IPCDispatcher`: explicit `(unified AP ID, destination module)` routes.
  Constructors accept independent `IPCEndpoint` implementations and limits;
  neither parser nor dispatcher knows a ROM hash, PC, chipset or MMIO base.
- `UnavailableConnectivityEndpoint`: the ALPS Q0 WMT STP-not-ready branch drops
  notifications without replying. Its only retained state is a saturating
  counter. No message bytes are retained or logged; unknown routes still fail.

The WMT factory binds reviewed sibling-MOLY IDs `(0x80000003, 0x404)`, not one
message ID or one image. This is an explicit ABI selection, not a claim of
automatic compatibility. Other routes require independently reviewed endpoints.

PCCIF accepts constructor injection of the dispatcher. The loader supplies a
fresh instance only to the MD0 PCCIF endpoint when opted in, without mutating
the shared SoC descriptor. Existing RPC/FS/control handlers remain unchanged.
The receive ring's existing read-pointer advancement is unchanged; this policy
does not write an ACK, response ring or interrupt. Transport receipt is not
application success. Unknown IPC still emits bounded envelope metadata.

Reference: local ALPS Q0 `common_main/linux/wmt_idc.c:54` implements the
STP-not-ready drop; sibling MOLY `ccci_ipc_if.h` and `ccci_ipc_module_conf.h`
define ILM and route IDs. These proprietary sources are not bundled. The older
[AP IPC driver](https://android.googlesource.com/kernel/mediatek/+/android-mediatek-sprout-3.10-marshmallow-mr2/drivers/misc/mediatek/eccci/port_ipc.c)
is supporting reference only, not proof of every newer firmware ABI.

Observed unchanged Lagos envelope: source 13, destination 0x404, SAP 0,
message 0x8000005f, AP ID 0x80000003, parameter length 21. The implementation
does not special-case these source/message values. Their semantic message name
is unresolved. Metadata capture alone does not prove full boot.

Tests cover multiple messages and synthetic routes, truncation and malformed
lengths, unsupported peer buffers, privacy, instance isolation/pickle state,
bounded counters, explicit native-only selection and real ring consumption
without response/IRQ writes. Saved-live acceptance remains separate from unit
tests and from real AP↔modem interoperability.
