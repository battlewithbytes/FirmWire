"""Evidence-bound MTK location resolution, independent of PANDA and debug files.

Profiles are reviewed assertions about named locations, not proof of a name's
semantics. Exact ROM identity, byte witnesses, unique matches and bounds prevent
accidental application to another build. No first-match or cross-image cache.
"""
import hashlib
import re

PROFILE_SCHEMA = "cockpit.mtk-symbol-profile/v1"
REPORT_SCHEMA = "cockpit.mtk-capabilities/v1"

# These are requirements of the current single-core rehosting implementation,
# not requirements imposed by a modem CPU or the vendor firmware format.
REHOSTED_GROUPS = {
    "core_sync_workarounds": ("corewait_addr_code", "INC_Initialize_corewait", "sync1_addr_code"),
    "nvram_table_workaround": ("ptr_logical_data_item_table", "nvram_ltable_init_code"),
    "l1_assert_workaround": ("L1D_CustomDynamicGetParam_assert",),
    "legacy_function_skips": ("SST_Secure_Algo", "SEJ_AES_HW_Kdf_Internal", "nvram_sec_check",
                              "PMIC_Read_All", "MML1_RF_Wait_us", "ccismc_submit_ior"),
    "task_layout_workarounds": ("ptr_sys_comp_config_tbl", "stack_init_comp_info"),
    "legacy_errc_assert_workaround": ("errc_evth_inevt_handler_assert", "errc_evth_inevt_handler_end"),
}
OPTIONAL_GROUPS = {
    "trace_filter_patch": ("tst_trace_check_ps_filter_off",),
    "named_error_logging": ("ex_reboot", "stack_system_error", "general_ex_handler",
                            "general_ex_vector", "INT_EnterExceptionForOtherCore", "ERC_System_Error"),
    "named_trace_logging": ("dhl_internal_trace_impl", "dhl_print", "dhl_print_string",
                            "kal_prompt_trace", "tst_sys_trace", "tst_sysfatal_trace"),
    "event_logging": ("NU_Set_Events",),
    "task_switch_logging": ("TCC_Task_Ready_To_Scheduled_Return",),
}


def _integer(value, label):
    if type(value) is not int:
        raise ValueError(label + " must be an integer")
    return value


def _compile(pattern, min_fixed=1):
    tokens = re.sub(r"\s+", "", pattern)
    if not tokens or len(tokens) % 2:
        raise ValueError("invalid byte pattern")
    parts, fixed = [], 0
    for i in range(0, len(tokens), 2):
        token = tokens[i:i + 2]
        if token == "??":
            parts.append(b".")
        elif re.fullmatch(r"[0-9a-fA-F]{2}", token):
            parts.append(re.escape(bytes.fromhex(token)))
            fixed += 1
        else:
            raise ValueError("only fixed bytes and ?? are supported")
    if fixed < min_fixed:
        raise ValueError("pattern has insufficient fixed-byte evidence")
    return re.compile(b"".join(parts), re.DOTALL), len(tokens) // 2


def _unique(data, pattern, start, end, align=1, min_fixed=1):
    compiled, width = _compile(pattern, min_fixed)
    if start < 0 or end > len(data) or start >= end:
        raise ValueError("pattern range outside ROM")
    if type(align) is not int or align < 1 or align & (align - 1):
        raise ValueError("alignment must be a positive power of two")
    found, pos = None, start
    while pos + width <= end:
        match = compiled.search(data, pos, end)
        if match is None:
            break
        pos = match.start() + 1  # include overlapping matches
        if match.start() % align:
            continue
        if found is not None:
            raise ValueError("ambiguous pattern")
        found = (match.start(), match.end())
    if found is None:
        raise ValueError("pattern not found")
    return found


class Resolver:
    def __init__(self, rom, base, soc, debug_info=None):
        self.rom, self.base, self.soc = rom, base, soc
        self.sha256 = hashlib.sha256(rom).hexdigest()
        self.symbols, self.sizes, self.evidence, self.unresolved = {}, {}, {}, {}
        for name, (address, size) in (debug_info or {}).items():
            if type(address) is int and type(size) is int and size >= 0:
                self.symbols[name], self.sizes[name] = address, size
                self.evidence[name] = {"source": "vendor-debug-info"}

    def _check_range(self, address, size):
        _integer(address, "address")
        _integer(size, "size")
        start = (address & ~1) - self.base
        if size <= 0 or start < 0 or start + size > len(self.rom):
            raise ValueError("symbol range outside ROM")
        return start

    def _add(self, name, address, size, evidence):
        self._check_range(address, size)
        if name in self.symbols and (address, size) != (self.symbols[name], self.sizes[name]):
            raise ValueError("conflicting resolution for " + name)
        self.symbols[name], self.sizes[name] = address, size
        self.evidence[name] = evidence
        self.unresolved.pop(name, None)

    def apply_profile(self, profile):
        if (profile.get("schema") != PROFILE_SCHEMA or profile.get("arch") != "mipsel"
                or profile.get("soc") != self.soc or profile.get("rom_base") != self.base
                or profile.get("rom_sha256") != self.sha256):
            raise ValueError("symbol profile identity/architecture/platform mismatch")
        entries = profile.get("symbols")
        if not isinstance(entries, list):
            raise ValueError("profile symbols must be a list")
        seen = set()
        # Validate in a staging resolver: a bad entry cannot partially apply.
        staged = Resolver(self.rom, self.base, self.soc)
        staged.symbols, staged.sizes = dict(self.symbols), dict(self.sizes)
        staged.evidence = dict(self.evidence)
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError("missing or duplicate profile symbol name")
            seen.add(name)
            evidence = entry.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("each location requires review evidence")
            size = _integer(entry.get("size"), "size")
            if "address" in entry:
                if "pattern" in entry:
                    raise ValueError("choose address witness or signature, not both")
                address = entry["address"]
                start = staged._check_range(address, size)
                witness = bytes.fromhex(entry.get("expected_hex", ""))
                if len(witness) < 8 or len(witness) > size or self.rom[start:start + len(witness)] != witness:
                    raise ValueError("address byte witness mismatch or too short")
                source = "reviewed-address-witness"
            else:
                # Whole-ROM signatures need substantially more evidence than
                # the tiny return instructions used inside known functions.
                loc = _unique(self.rom, entry.get("pattern", ""), 0, len(self.rom),
                              entry.get("align", 2), min_fixed=16)
                delta = _integer(entry.get("offset", 0), "offset")
                if delta < 0 or delta >= loc[1] - loc[0]:
                    raise ValueError("signature offset must be inside matched bytes")
                mode = entry.get("mode_bit", 0)
                if type(mode) is not int or mode not in (0, 1):
                    raise ValueError("invalid instruction mode bit")
                address = (self.base + loc[0] + delta) | mode
                source = "unique-rom-signature"
            staged._add(name, address, size, {"source": source, "evidence": evidence})
        self.symbols, self.sizes, self.evidence = staged.symbols, staged.sizes, staged.evidence

    def scoped_patterns(self, patterns):
        for name, entry in patterns.items():
            if name in self.symbols:
                continue
            parent = entry.get("within")
            try:
                if parent not in self.symbols:
                    raise ValueError("missing containing function: " + str(parent))
                start = self._check_range(self.symbols[parent], self.sizes[parent])
                end = start + self.sizes[parent]
                loc = _unique(self.rom, entry["pattern"], start, end, entry.get("align", 1))
                point = loc[1] + entry["offset_end"] if "offset_end" in entry else loc[0] + entry.get("offset", 0)
                if point < start or point >= end:
                    raise ValueError("derived location outside containing function")
                self._add(name, self.base + point, min(4, end - point),
                          {"source": "unique-scoped-pattern", "within": parent})
            except (ValueError, KeyError) as exc:
                self.unresolved[name] = str(exc)

    def capabilities(self, mode):
        if mode not in ("native", "rehosted"):
            raise ValueError("unknown boot mode")
        groups = {}
        for required, catalog in ((True, REHOSTED_GROUPS), (False, OPTIONAL_GROUPS)):
            for group, names in catalog.items():
                missing = []
                for name in names:
                    try:
                        self._check_range(self.symbols.get(name), self.sizes.get(name))
                    except ValueError:
                        missing.append(name)
                groups[group] = {"required": required and mode == "rehosted",
                                 "available": not missing, "missing": missing,
                                 "enabled": mode == "rehosted" and not missing}
        used = set(n for cat in (REHOSTED_GROUPS, OPTIONAL_GROUPS) for names in cat.values() for n in names)
        return {"schema": REPORT_SCHEMA, "rom_sha256": self.sha256, "soc": self.soc,
                "boot_mode": mode, "debug_symbol_count": sum(e["source"] == "vendor-debug-info" for e in self.evidence.values()),
                "resolved": {n: {"address": self.symbols[n], "size": self.sizes[n], **self.evidence[n]}
                             for n in sorted(used & self.symbols.keys())},
                "unresolved_patterns": self.unresolved, "capabilities": groups,
                "startup_locations_ready": not any(g["required"] and not g["available"] for g in groups.values()),
                "machine_initialized": False, "cpu_execution_observed": False,
                "task_progress_verified": False, "firmware_handshake_verified": False,
                "limitations": ["Native mode skips all symbol-based rehosting patches and named hooks.",
                                "Existing platform memory/peripheral models remain approximate.",
                                "A location preflight is not successful hardware initialization or boot."]}
