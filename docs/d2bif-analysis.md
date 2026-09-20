# 93xx D2BIF storage experiment

`--mtk-loader-d2bif 93xx-storage-analysis` explicitly enables an **unverified**
two-word storage model. Default is disabled; native boot mode is required.
The loader maps physical 0xab820000..0xab820fff, but only aligned 32-bit
accesses to +0x28/+0x4c are accepted. Reads before writes fail because reset
values are unknown. Other offsets, including command +0x3e8, are rejected.
No broad register-file fallback, DMA, completion or IRQ is supplied.

The reusable `ConfigurationRegisters` core supplies validation and storage.
The family frontend supplies offsets and bounded, payload-free observations.
Its constructor accepts any base; no firmware hash, PC, expected word or
instruction count affects device behavior. Both the retained-value behavior
and lack of side effects remain hypotheses, labeled in startup logs,
capabilities and observations with `analysis_only: true`,
`semantics_verified: false`, `boot_verified: false`.

## Evidence and limits

- Older MOLY MT6768 `reg_base_MT6768.h` identifies AB820000 as BIGRAM D2BIF;
  BB820000 is a separate CPU address view. This model adds only the physical
  AB820000 aperture, not a second independent device or an invented alias.
- Lagos and Coful initializers write 56 to both offsets. The sibling routine
  is `EL1D_TC_HW_Init`; WCDMA `ul1d_dsp_rake_d2bif_enable` writes the same pair.
- `ul1d_rxdfe_dbg_set_d2bif` writes 47 to +0x28. Values are therefore not
  acceptance tokens and are never hardcoded into the device.
- Target diagnostic code reads both words. A matching bounded instruction
  neighborhood in the sibling ELF lies in `UL1D_DBG_D2B_LTE_CTRL_CON`, which
  forwards those values into DHL logging. This supports investigating readable
  configuration, but proves neither reset/readback values nor side effects.
- Distinct commands and status accesses occur elsewhere in this block. They
  remain unsupported. The neighboring BIGRAM_REG arbitration fields are not
  D2BIF register definitions.

Cockpit's `mips16-d2bif-mmio` profile gathers hash-checked prepared-ROM Ghidra
evidence. It is bounded, encoding-specific and straight-line, not exhaustive
xrefs or a verified load map. Proprietary images, reference objects and the
synthetic identity remain local-only.

## Tests

`tests/test_d2bif.py` covers arbitrary words, unknown reset, isolated relocated
instances, strict commands/widths, bounded observations and native-only policy.
Set `FIRMWIRE_TEST_NATIVE_D2BIF=1` for `tests/test_d2bif_native.py` in the
development PANDA environment. Its synthetic guest validates storage, not
hardware semantics. Actual unchanged-firmware progress is a separate Cockpit
experiment; no successful modem boot is implied by these tests.
