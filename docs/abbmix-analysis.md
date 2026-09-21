# Selected-result ABB calibration analysis

This is a **device**, not a firmware return hook. The observed guest sets up
registers, raises a trigger, polls two completion bits, clears the trigger,
selects results, and copies them into its own RAM. No guest PC, instruction,
function return value or result-table RAM location appears in the device model.

`hw/abbmix.py` is the reusable halfword register/state model.
`hw/AbbMixPeripheral.py` supplies the MMIO adapter. `abbmix_profile.py` validates
an explicit, ROM-bound JSON profile. No profile is enabled by default.

## Contract and deliberate assumptions

The layout declares trigger/selector/status offsets, one trigger and ready bit,
result width, and selector shift/width. Ordinary storage, initial values, setup
write prerequisites and each bank's result array are explicit profile inputs.
The constructor supports relocated offsets, one to four banks, and up to 64
results per bank. Invalid/unknown accesses stop; there is no generic success or
write fallback. Layouts cannot overlap status with writable configuration.

- Reset clears ready, work, latched results, counters and setup-write history.
- A rising trigger starts a new operation, clearing previous completion/results.
- A complete explicit result backend plus all declared setup writes completes
  synchronously. This is a **software-analysis timing/result assumption**, not
  measured analog behavior. Setup checks write presence, not physical validity.
- Without a backend or prerequisites, work stays pending and reads do not advance
  it. Invalid result data is explicitly zero with ready clear.
- Clearing the trigger cancels pending work, but completed results and ready
  remain latched for the subsequent guest copy. This retention is a labelled
  analysis policy, not a verified reset/clear specification.
- Selector writes change which latched result each status word exposes. The
  ready bit is separate from the low result bits. Counters record selected reads.
- Repeated high trigger writes do not restart. A new low-to-high edge does.

Facts explicitly report `completion_fabricated`, `analysis_only`, no analog
model, no routed IRQ, no verified silicon and no verified full boot. Snapshots
are unsupported; peripheral serialization is refused. Use cold boots only.

## Selection and current mapping

`--mtk-loader-abbmix_analysis_profile <path>` requires native boot and an exact
ROM digest match. The schema is `firmwire.abbmix-analysis/v1`, with exact keys:
`rom_sha256`, `source` (`analysis-assumption`), `reason`, `base`, `size`, `layout`,
`registers`, `prerequisites`, `results`, `modeled_range`, and `schema`.
`registers` has canonical decimal offset keys and explicit 16-bit values or null.
`results` is null (unresolved backend) or one full width-bounded array per status
register. Duplicate JSON keys, malformed arrays and overlapping layouts fail.

The current machine integration replaces only a page-aligned portion of its
existing `0xa6190000..0xa619dfff` ABB window, preserving prefix/suffix RAM without
overlap. That enclosing window is a reviewed machine mapping, not an assumption
that every modem has the same address. New family maps need separate review;
the device and schema themselves do not contain that address or a ROM identity.
`modeled_range` is an explicit half-open pair of offsets within the page. Only
that range dispatches to the strict device. Other addresses in the page retain
the existing zero-initialized RAM semantics through FirmWire's reusable
`PassthroughPeripheral`; this is labelled legacy backing, not invented register
behavior. Crossing the boundary is rejected, not partly executed. Observation
reports the modeled range and separate backing access counts. Outside the page,
pre-existing plain-memory behavior is unchanged.

The first live trial caught an overly broad whole-page interception: a read at
`0xa619da0c` predates the reviewed calibration routine. It failed strictly before
any operation completed. The corrected profile models only offsets `0x800..0x85f`
inside the `0xa619d000` page. It does not add a fabricated calibration response
for the unrelated register. The original fixture requires the earlier `f46fee7`
profile parser; the revised profile explicitly includes `modeled_range`.

Cockpit carries the example Lagos fixture: two banks of sixteen explicitly
synthetic, distinct 12-bit values; trigger bit 0 and ready bit 13; selector in
bits 15:12. Lagos/Coful disassembly independently supports the access protocol,
not real calibration values, completion timing or analog fidelity. Coful has not
been live-booted with this profile. No DSP instruction emulation is introduced.

## Tests

`tests/test_abbmix.py` covers relocated layouts, every selected result, isolation,
missing backend/setup, repeated trigger, stop/reset, invalid access widths,
unknown accesses, profile identity/malformed data, mapping partition and snapshot
refusal. `tests/test_abbmix_native.py` runs actual MIPS halfword loads/stores at
two unrelated bases, copying both banks to guest RAM. Enable it with
`FIRMWIRE_TEST_NATIVE_ABBMIX=1` in the existing `firmwire-cockpit:irq` container.

The selected full regression suite passes **324 tests / three skipped**, with
native gates enabled and the external-download test excluded. The unchanged
firmware experiment is recorded separately in Cockpit; unit/native synthetic
success is not proof of vendor firmware boot. A ninth focused test additionally
covers preserved neighboring RAM at several widths, strict in-range unknowns,
out-of-bounds accesses and crossings between RAM and device state.
