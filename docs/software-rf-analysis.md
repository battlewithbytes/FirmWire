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
- The native execution callback advances the sequencer by 1024 logical ticks
  every 1024 completed guest blocks. This is an explicit experimental scheduling
  policy, **not** a model of silicon cycles or microseconds. Polling a register
  does not itself advance the sequencer or fabricate responses. It remains
  independent of whether a read-only observer is enabled.
- Trigger 0 only; trigger strobes self-clear. Unsupported trigger-1, global-offset
  selector-1, MIPI configuration and active clear/retrigger fail explicitly.
- No physical RF, analog calibration results, DSP execution, modem task progress,
  AP handshake or verified full boot is provided by this software target.

The existing GCR timer is a separate read-incrementing approximation. A future
shared guest-time model is required for timing fidelity; do not equate this
sequencer's logical ticks with that timer's values.

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

## Tests

`tests/test_mtk_rf_serial.py` tests framing, explicit identities, storage/readback,
reset, unknown reads, negative inputs and profile validation.
`tests/test_mtk_software_rf.py` checks loader gating and the integrated adapter.
`tests/test_mtk_hwpor.py` covers the pure digital sequencer.

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
