# Shared PMIC target: explicit digital analysis, not silicon emulation

The PMIC is one device even when firmware accesses it through different buses.
`PmicTarget` (`hw/pmic.py`) is the transport-independent read/write/reset/facts
contract. `PmicWacsControl` implements the reviewed WACS command/status/clear
interface; `PmicBsiWriteTarget` (`hw/pmic_serial.py`) implements the observed
16-address/16-data BSI write path. Both receive **the same target instance**.

This is programmatic constructor opt-in only. No ROM/chipset auto-selection,
new boot-recipe switch, hardware identity, or live-image register defaults are
provided. Existing boot profiles still use the legacy wrapper. The new classes
are not evidence that a modem has booted, nor a verified MT6357/MT6358 model.

## Minimal composition

```python
from firmwire.vendor.mtk.hw.pmic import PmicRegisterMapAnalysis, PmicRegisterSpec
from firmwire.vendor.mtk.hw.PMICPeripheral import PMIC_WRAP_Periph
from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral

# Synthetic example only; these are NOT Lagos addresses/reset values.
target = PmicRegisterMapAnalysis({
    0x24: PmicRegisterSpec(reset_value=0x1357),
    0x42: PmicRegisterSpec(reset_value=0xa5c0, write_mask=0x000f),
    0x60: PmicRegisterSpec(),  # initially unknown, not implicitly zero
}, reason="synthetic cross-transport experiment, not measured hardware")

wrapper = PMIC_WRAP_Periph("wacs", 0x600000, 0x2000,
    pmic_target=target, firmwire_machine=machine)
bsi = BSIImmediatePeripheral("bsi", 0x400000, 0x9000,
    bsi_mode="pending", pmic_target=target, pmic_port=9,
    firmwire_machine=machine)
```

BSI can compose the PMIC alongside explicit software RF and idle-MIPI targets;
port conflicts are rejected. No port number lives in the PMIC target or adapter.
The existing `software-rf` mode still requires its ROM-bound RF profile. In
plain `pending` mode only explicitly supplied endpoints are connected.

`PmicRegisterMapAnalysis` supports only bounded, caller-selected plain registers:
16-bit values, explicit reset value or unknown, write mask, and read permission.
Masked writes preserve unwritable bits; preserving unknown bits is unresolved.
A full-width accepted write may establish an initially unknown word. Unlisted,
read-only writes and unreadable/unknown reads do not create state. Reset values
are labelled analysis assumptions, never inferred from a legacy zero fallback.
There is no implicit HWCID, protection-key state, SET/CLR alias, oscillator lock,
voltage-ready signal, interrupt or calibration result. A reviewed hardware-family
subclass of `PmicTarget` can add those behaviors without changing transport code.

## Transport and lifecycle contracts

WACS command: write flag bit 31, address `(command[30:16] << 1)`, data low 16
bits. The write flag is excluded from the address (including high-address tests).
Status carries data low 16, FSM bits 18:16, and a configurable init-done bit
(default 21, existing SoC callers can supply 22). Other status fields are not
modelled. Completion is synchronous analysis behavior, not bus timing.

Supported writes return idle (FSM 0). Completed reads retain their value in
FSM 6 until valid-clear value 1. Unsupported requests remain FSM 2, retaining
their reason; they never return a completed zero read or accept another command.
Polling does not call the target again. `retry_pending()` is an explicit backend
operation; it is not invoked by guest polling. A retry is safe only for targets
whose unresolved result has no side effects. Invalid widths/registers, commands
while busy, and invalid ACKs are rejected without clearing state.

BSI only completes an accepted non-extended write. Read encoding is unresolved,
so no BSI read response is synthesized. Nonzero second words are unsupported.
MIPI length registers do not select PMIC behavior: the observed RF sender leaves
them stale. Existing controller completion and busy handling remain in charge.

Each transport's reset clears only its local request/latch/counters. It **does
not reset the shared target**. The machine/composition owner must explicitly
reset the target and the controllers together for a whole-device reset. This
avoids one controller silently resetting a device still used by another.

Per-peripheral snapshotting can clone shared objects independently. Until the
machine serializer preserves target identity, both opt-in peripheral adapters
reject snapshot creation. Legacy snapshot behavior remains unchanged. Snapshot
support and concurrent multithreaded target access are not claimed.

## Evidence and verification

The target-image BSI sender and DCXO constructor are covered by Cockpit's
Lagos/Coful Ghidra/reference gates; differing firmware masks are retained. This
component contains none of those image PCs, hashes, addresses or payloads.

Older MOLY source corroborates WACS framing/state numbers, but not analog device
behavior: `mcu/common/driver/devdrv/pmic_wrap/src/pmic_wrap_v2.c:1098-1130` and
`:1800-1848`; `inc/mt6768_pmic_wrap_hw.h:400-411` and `:472-476`. The latter uses
init-done bit 22, demonstrating why that field remains a layout parameter.

Run the pure tests without FirmWire dependencies:

```sh
python -m unittest discover -s tests -p test_pmic.py -v
```

Run composition/native tests in the existing development container:

```sh
docker run --rm -v "$PWD:/work:ro" -w /work \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work:/opt/panda-python \
  -e FIRMWIRE_TEST_NATIVE_PMIC=1 --entrypoint python firmwire-cockpit:irq \
  -m unittest discover -s tests -p 'test_pmic*.py' -v
```

Tests cover shared readback, masks/unknown bits, independent devices/channels,
ACK ordering, local/device reset separation, malformed results, unsupported
directions, explicit configuration and snapshot refusal. The native test runs
unchanged synthetic MIPS instructions at two bases/ports with different payloads,
observing successful BSI-write/WACS-read coherence and unresolved unknowns.
No vendor firmware, PMIC reset table or hardware identity is needed by these tests.

## Still required for Lagos

Identify/select the PMIC variant; review ordinary versus protected/aliased DCXO
fields; supply explicit evidence or labelled assumptions for necessary reset
values; compose one target into the real machine's WACS and BSI construction.
Then rerun unchanged firmware and verify DCXO pre-init/init return. Simply
allowing every PMIC write or copying the old wrapper's zeros into a register
map is not that validation. No new mtkloader parser requirement is established.
