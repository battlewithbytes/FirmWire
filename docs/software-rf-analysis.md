# Opt-in software RF startup analysis

The native MTK loader accepts `--mtk-loader-bsi mt6768-software-rf` **only with**
`--mtk-loader-rf_analysis_profile /path/to/profile.json`. The default remains
`disabled`; observe/pending/capture-writes modes retain their prior behavior.
Rehosted boot, missing profiles and mismatched ROM digests are rejected before
peripheral mapping. The profile is recorded in the capability report and the
peripheral facts; startup logs explicitly label the software substitution.

Example schema (the digest and values must be explicit experiment choices):

```json
{
  "schema": "firmwire.software-rf/v1",
  "name": "my-reviewed-software-experiment",
  "analysis_only": true,
  "rom_sha256": "<64 lowercase hex characters for the extracted ROM>",
  "assumptions": "Describe why these software values and ports were chosen; not silicon evidence.",
  "ports": {"0": {"chip_id": 8, "eco": 0}}
}
```

No chipset-name or image-name heuristic chooses the target. Chip/ECO fields are
four-bit integers. Every configured port receives its own target instance;
unconfigured ports remain unresolved. The example values are not universal MTK
values. Cockpit has separately hash-bound Lagos and Coful experiment profiles.

## Modeled behavior and explicit assumptions

- Immediate BSI and HWPOR share the same serial bus and software register state.
- CW0 reads return the explicitly configured chip/ECO nibbles with upper bits
  zero. The observed CW0 SOR write (`0x80000`) resets software register storage,
  preserving the configured identity. This is an analysis reset policy, not
  verified silicon behavior; other CW0 writes are unsupported.
- Other accepted RF writes store their 20-bit payload at the addressed control
  word. Reads return an actually written value or an explicitly declared reset
  seed (below). **All other reads stay unresolved**, not zero-filled. Reserved framing and extended transfers are
  rejected without creating a completion.
- HWPOR consumes guest-programmed event/slot registers. No mtkloader output is
  injected into the device or guest RAM. Backend write handling drives completion;
  an absent/unresolved backend leaves work pending.
- Software RF synchronizes POR to the existing GCR analysis counter: 75 HWPOR
  ticks per GCR unit, as in the reviewed event writer. The earlier independent
  1024-block batches are removed. Due events run before a GCR value or BSI access
  is exposed; BSI polls and observer snapshots do not advance the counter or
  retry unanswered reads. **GCR timer reads still advance the legacy counter**:
  this fixes relative ordering, not free-running/silicon timing fidelity.
- Trigger 0 only; trigger strobes self-clear. Unsupported trigger-1, global-offset
  selector-1, MIPI configuration and active clear/retrigger fail explicitly.
- No physical RF, measured analog calibration results, DSP execution, modem task progress,
  AP handshake or verified full boot is provided by this software target.

`rf_guest_clock` capability facts and the BSI `clock_policy` record the source,
75:1 conversion, and both polling behaviors explicitly. GCR low words wrap at
32 bits while its analysis epoch remains unwrapped for scheduling; its aliases
retain the legacy same-counter interpretation, not independently verified OS
timer semantics. Software RF requires cold restart: snapshot flags are refused.
A future validated free-running virtual clock is still needed. Two attempted
global guest-block sources exposed an earlier PMIC timeout and were not retained.

## Optional software reset state (analysis assumptions only)

A port may additionally declare `reset_registers`, at most 64 canonical decimal
address keys from 1 through 1023 (CW0 stays the separate identity/reset contract).
Each entry requires a 20-bit integer `value`, `source: "analysis-assumption"`
and a nonempty `reason`. For example, an unrelated synthetic test seed is:

```json
{"chip_id": 8, "eco": 0, "reset_registers": {
  "341": {"value": 214375, "source": "analysis-assumption", "reason": "synthetic storage test, not measured silicon"}
}}
```

Initialization, controller reset and CW0 SOR restore these declared values and
discard guest writes. Writes may subsequently overwrite them. Inputs and facts
are copied; targets on different ports remain isolated. `registers_written`
counts actual guest-written registers, not seeds. Facts retain each seed and
its reason, and reset-seeded read completions have a distinct assumption reason.
Omitting the map preserves the original unresolved-read behavior.

This is a caller-supplied storage experiment, not extracted register defaults or
calibration emulation. It must not be used to claim analog completion/accuracy.
In the reviewed Lagos code, CW367 (`0x16f`) is backed up then restored after
calibration. That establishes storage usage, **not its silicon reset value**.

## Optional synthetic MT6177M RCAL responses

Each port may explicitly select a `calibration` object:

```json
{"kind": "mt6177m-rcal-analysis/v1", "source": "analysis-assumption",
 "reason": "Synthetic trial values, not measured RF calibration",
 "cw10": 541200, "cw11": 473550, "trim5": 16}
```

CW10/CW11 must be integers in 0..0xfffff; `trim5` must be in 0..31. These bounds
come from the decoded consumer's field widths, **not physical validity limits**.
The factory never chooses this variant from a phone name or chip nibble. Unknown
kinds and conflicting reset seeds for CW9/10/11 are rejected. Existing profiles
without this object retain their storage-only behavior.

The guest must write CW8=`0x81c00`, CW8=`0x81c01`, CW9=0, CW12=0 in that control
order. The software model completes synchronously on the final setup write;
it does not claim to enforce the firmware's waits or emulate analog timing.
Before completion, result reads remain unresolved (and polling does not retry
them). Afterwards CW10/CW11 return the configured words and CW9 returns
`trim5 << 10`, overriding its stored control write of zero. Unrelated trim writes
leave results stable. A new or unexpected write to CW8/9/10/11/12 invalidates or
restarts setup; CW0 SOR and controller reset clear progress. Coful's additional
CW11 setup write invalidates this M-variant sequence rather than silently using
it as an L-variant model. Per-port instances remain isolated.

Facts expose `calibration_emulated: true`, the exact configuration, setup phase,
completion/read counts and synthetic timing; `analog_calibration_verified` and
`silicon_verified` remain false. This object is **only RCAL**; CW77 remains
unresolved unless the separate LDO model below is explicitly configured.
No firmware instructions or runtime calibration tables are patched/preloaded.

## Optional synthetic MT6177M LDO responses

A port may additionally select `ldo_calibration` independently of RCAL:

```json
{"kind": "mt6177m-ldo-analysis/v1", "source": "analysis-assumption",
 "reason": "Synthetic trial trims, not measured RF calibration",
 "trims": {"8": 7, "64": 19}}
```

`trims` is a nonempty subset of the reviewed selectors, keyed by canonical
decimal strings. Missing selectors do not acquire a default result. Selectors
`0x8` (THADC) and `0x10000` (TTG) accept four-bit trims; `0x100`, `0x400`,
`0x200`, `0x80` (TX), `0x800`, `0x4000`, `0x8000` (STX), `0x80000`, `0x40000`,
`0x20000` (SRX1), and `0x40`, `0x20`, `0x10` (RX) accept five-bit trims.
These are digital consumer field widths, not physical validity ranges. Unknown
kinds/selectors, boolean values, out-of-width trims and CW77 reset seeds fail
validation. The model is not automatically selected from identity or ROM name.

The reviewed sequence is CW15=`0x5800`, CW75=`0x80000`, optional CW77 clear-phase
read, CW15=`0x1800`, CW76=selector, CW75=`0x40000`, CW77 result read.
THADC instead triggers with `0x60000`. RX selectors `0x40`/`0x20` use CW15
`0x7800`/`0x3800`. Intervening unrelated analog-register writes are storage-only.
The clear-phase read returns an explicitly assumed zero; the original consumer
overwrites that value before using the result. Reads do not advance setup.

Completion is synchronous on the reviewed trigger and returns `trim << 15`.
The firmware's 20/140-unit waits are not enforced as analog latency. The selected
result is latched: clearing CW76 after trigger preserves it because RX does that
before readback. Other unreviewed control/result writes invalidate state;
CW15=`0x800` disables it. CW0 SOR and controller reset clear all model progress.
Per-port/device state is isolated. Facts expose exact configuration, current
phase, latched selector, completion and read counts, and completed selectors.
All physical/analog verification flags stay false.

Tests cover every selector, zero/max/varied trims, invalid sequences, mode and
trigger mismatches, omitted selectors, reset, input/fact copying and isolation.
Native guest tests independently exercise THADC and RX with distinct trim sets,
including selection clearing before read. Without the separate model below, CW447 (RX RC) remains unresolved;
this model does not implement subsequent RX RC, TPD, DSP or RF waveforms.

The initial Cockpit LDO-profile attempt stopped
before calibration at an early POR read. HWPOR later supplied the register but
the immediate read remained unresolved. The adapter characterization test
reproduces this both with and without LDO configured. That attempt used separate
batched-block and read-driven clocks; shared-counter synchronization is the
follow-up above. Do not
interpret the model/native unit tests as proof of a successful firmware LDO run.

The subsequent Cockpit shared-counter run (`drdi-preload-XWBp9A`) does qualify
the LDO milestone: all 15 cycles, firmware-written trim commands and six ordered
routine returns pass saved-live acceptance. It then times out on unsupported
RX RC CW447. This is still a failed full boot, not analog RF or AP communication.

## Optional synthetic MT6177M RX RC response

A port may independently select `rx_rc_calibration`:

```json
{"kind": "mt6177m-rx-rc-analysis/v1", "source": "analysis-assumption",
 "reason": "Synthetic test trim, not measured RF calibration", "trim6": 23}
```

Only integers 0..63 are accepted (not booleans). This is a consumer field width,
not an analog validity range. CW447 reset seeds conflict with this model and are
rejected. Nothing selects the model automatically from a phone name or identity.

The reviewed setup is CW1=`0x112a0`, CW320=0, CW321=0, CW467=`0x2c01`,
CW1=`0x212a8`, in that order. CW447 then returns `trim6 << 14`. Completion is
synchronous on final setup; the consumer's 60-unit delay is not modeled as analog
latency. Unrelated writes do not advance setup. Partial/wrong/reordered sequences
stay unresolved. CW447 guest writeback, restore, or unexpected relevant control
writes invalidate the result. Stored guest writeback is not a fallback calibration
result. CW0 SOR/controller reset clears the model; per-port state is independent.

Facts record the exact assumed configuration, phase, completions, reads and
synthetic timing; analog/silicon verification remains false. The firmware itself
duplicates the six-bit trim and writes its own calibration table. The model knows
no instruction addresses, table locations, image names or polling counts.

This contract does not provide RX TPD backup state/results, DSP responses, RF
waveforms or an AP handshake. Unknown reads remain pending, with no automatic
retry of reads issued before setup. The opt-in native test checks three distinct
trims (including zero/max), guest writeback, earlier pending reads and isolation.

Cockpit's unchanged-Lagos run `drdi-preload-BwUcfz` verifies one result consumed,
the exact firmware-owned RX RC table store, RX RC return and RX TPD entry. It
then waits on unwritten CW469 (`0x1d5`), times out and hits the existing PCCIF
assertion. RX RC acceptance passes while full modem boot remains false.

## Optional synthetic MT6177M RX TPD response

A port may independently select `rx_tpd_calibration`:

```json
{"kind": "mt6177m-rx-tpd-analysis/v1", "source": "analysis-assumption",
 "reason": "Synthetic result trial, not measured RF calibration",
 "cw423_trim4": 5, "cw429_trim4": 11}
```

The two trims must be integers in 0..15, not booleans. Those limits reflect the
consumer's four-bit field extraction, not physical validity. CW423/CW429 reset
seeds conflict with the result model and are rejected. It does not supply any
CW469/CW472 backup defaults: those require actual guest writes or separately
labelled `reset_registers` assumptions. Supplying backup seeds alone does not
enable calibration results, and selecting the result model does not seed backups.

The reviewed control sequence is:
CW320=1, CW321=1, CW322=`0x8051`, CW324=`0x98b1`, CW326=`0x880`,
CW399/CW400=`0x3d7a`, CW495=2, CW500/CW501=`0x1b780`, CW128=1,
CW130=`0x29276`, CW131=`0x276a9`, CW179=`0x4b0e`, CW413/CW414=`0x7f8`,
CW469/CW472 low ten bits=`0x96`, CW1=`0x212a8`, CW6=`0x414`.
The upper ten bits of CW469/CW472 are unconstrained retained storage, not a
selector or hardcoded fixture. All other setup payloads must match exactly.

Only after the final trigger do CW423/CW429 return their configured trims shifted
left eleven bits. Completion is synchronous; the firmware's 250-unit delay is
not treated as an analog timing guarantee. Reads do not advance setup. Unrelated
writes leave state alone. Relevant wrong writes, result writes or CW6=`0x384`
disable/restore invalidate it. SOR/controller reset clears it and per-port state
is independent. Early pending reads are not retried automatically.

The guest retains control of backup read/modify/write, low-bit clearing, restoring
CW320/CW321, and building the two command-table words. No firmware instruction or
runtime table is patched. The model is never selected by a PC, image hash, phone
name or inferred chip identity. Unknown stages and MT6177L compatibility remain
unimplemented. Facts retain assumed config, progress and per-result read counts;
analog/silicon verification is always false.

Cockpit's unchanged-Lagos run `drdi-preload-o2RzXd` verifies both TPD results,
the two firmware-owned table stores, TPD return, table-send return and enclosing
calibration return. No RF read remains pending. A later data-bus exception at
reported PC `0x901d2be6` still fails full boot; that mapping/access boundary is
separate from the synthetic calibration contract.

## Optional unpopulated MIPI line (not RF hardware emulation)

An unanswered serial read must not be repaired by forcing the controller ready.
The optional top-level `idle_mipi_ports` map instead connects an explicitly
assumed unpopulated bus to the existing controller/backend completion boundary:

```json
"idle_mipi_ports": {
  "3": {
    "kind": "standard-mipi-idle-line-analysis/v1",
    "source": "analysis-assumption",
    "reason": "Experiment with an unpopulated idle-low MIPI bus; not measured electrical behavior.",
    "idle_level": 0
  }
}
```

This fragment belongs beside `ports` in the existing ROM-bound software profile.
It is absent by default. Canonical decimal port keys 0..15 must not overlap RF
targets; no port is populated automatically. Each endpoint is independent.

`IdleLineMipiTarget` accepts only standard read/write framing reviewed against
the firmware's frame constructor: odd-parity thirteen-bit header; read command
3 with nine sampled response bits (`length0=0x8000c`), or write command 2 with an
odd-parity nine-bit payload (`length0=21`). The second length must be zero;
extended, locked, malformed or other transfers remain unresolved.

Reads sample nine copies of the configured line level (0 or 1), including the
parity position. No slave parity, identity, calibration or register value is
generated. Writes have no target-side effects and do not create readback storage.
The controller completes supported transactions synchronously through its usual
backend/ack path. This does not establish electrical timing, silicon behavior,
successful RF operation, or full modem boot. High is a separate pull-level test,
not a promise that a guest interprets it as "absent". A real device requires a
different `SerialTarget` implementation and reviewed board population.

The profile and endpoint facts expose the assumption, counts and bounded frame
history; startup logs label `IDLE MIPI LINE ANALYSIS`. Unconfigured ports still
remain pending. Controller facts also retain `pending_blockers`, bound to each
bank/sequence until completion/reset, so rejected retries cannot erase the
original reason by rotating the ordinary event history. These diagnostics never
retry or complete a transaction.

## Tests

`tests/test_mtk_rf_serial.py` tests framing, explicit identities, storage/readback,
reset, unknown reads, negative inputs and profile validation.
`tests/test_mtk_ldo.py` covers the separate LDO sequence/result contract.
`tests/test_mtk_rx_rc.py` checks all 64 RX RC trims, partial/wrong/reordered
sequences, invalidation, resets, isolation, disabled behavior and strict config.
`tests/test_mtk_rx_tpd.py` checks all 256 result pairs, every omitted/wrong setup
word, masked backup preservation, invalidation, resets, disabled behavior and
strict config. Native guest tests vary backup seeds and result pairs, perform
the actual masked read/modify/write and verify early/disabled reads stay pending.
`tests/test_mtk_software_rf.py` checks loader gating and the integrated adapter.
`tests/test_mtk_hwpor.py` covers the pure digital sequencer.
`tests/test_mtk_guest_clock.py` tests relative deadlines across 1024 counter
origins, stalled backends, invalid sources and time reversal. Native timer-wait
tests use two starting phases/payloads and verify an unknown read stays pending.

With the development PANDA runtime, `FIRMWIRE_TEST_NATIVE_BSI=1` enables
`tests/test_mtk_bsi_native.py`: real synthetic guest instructions issue reset,
read identity, program and trigger an event, poll completion and read back the
written word. It tests two distinct identity/ECO settings, isolates another
device instance, and confirms an unwritten read stays pending.

Set `FIRMWIRE_RF_POR_INVENTORIES` to a JSON list of the saved Lagos/Coful
`rf-por-tables.json` paths for the optional corpus checks. They program every
extracted event/slot and verify all three table variants through the same core
and adapter. This test driver is not a production table-preload shortcut, and
the fixtures do not establish silicon identity or boot success.
