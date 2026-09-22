## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
import firmwire.loader
import firmwire.vendor.mtk.soc
import filetype
import logging
import struct
import lzma
import csv
import sys
import lz4.frame
import re
import pickle
import json
import hashlib

from io import BytesIO
from os import stat

import filetype
from tarfile import TarFile
from avatar2 import *
from pathlib import PurePath

from firmwire.hw.soc import get_soc
from .mtkdb.parse_mdb import readCATD
from .mtkdb.parse_lted import readLTED
from .pattern import PATTERNS
from .resolution import Resolver
from .machine import MT6878Machine
from .hw import *
from .hw.MML2MMUPeripheral import MML2MMU93Peripheral
from .hw.BSIPeripheral import BSIImmediatePeripheral
from .hw.idc_uart import MTKIDCUARTPeripheral
from .hw.idc_control import MTKIDCControlPeripheral
from .hw.d2bif import MTKD2BIFStorageAnalysisPeripheral
from .hw.mipi import MTKMipiInitCapturePeripheral
from .hw.bsi_scheduler import MTKBsiSchedulerCapturePeripheral
from .hw.lte_timer import (MTKLTETimerRRPeripheral, MTKLTETimerControlPeripheral,
                           MTKLTETimerInitStorageAnalysisPeripheral, MTKLTETimerGroupCancelAnalysisPeripheral,
                           MTKLTETimerEventCancelAnalysisPeripheral)
from .hw.PCCIFPeripheral import PCCIF_Periph
from .hw.ccci_ipc import unavailable_wmt_dispatcher
from .hw.ccci_ports import ClosedAPPorts
from .ccci_mailbox_profile import build_mailbox_profile
from firmwire.vendor.mtk.consts import ROM_BASE_ADDR

MAGIC = 0x58881688
MAGIC2 = 0x58891689

DBG_INFO_NAME = "md1_dbginfo"
DBG_DB_NAME = "md1_mddb"
MAIN_IMG_NAME = "md1rom"

log = logging.getLogger(__name__)


class MDFileException(Exception):
    def __init__(self, message):
        super().__init__(message)


class MTKSection:
    def __init__(self, loader, name, length, maddr, mode, header_start, data_start):
        self.loader = loader
        self.name = name
        self.length = length
        self.maddr = maddr
        self.mode = mode
        self.header_start = header_start
        self.data_start = data_start

    @property
    def data(self):
        with open(self.loader.md1img, "rb") as f:
            f.seek(self.data_start)
            data = f.read(self.length)
        return data

    def to_file(filename):
        with open(self.loader.md1img, "rb") as f:
            f.seek(data_start)
            data = f.read(self.length)
        with open(filename, "wb") as f:
            f.write(data)

    def __repr__(self):
        return f"MTKSection {self.name} with 0x{self.length:x} bytes"


class MTKLoader(firmwire.loader.Loader):
    NAME = "mtk"
    LOADER_ARGS = {
        "ccif_notifications": {
            "type": str, "choices": ["disabled", "ring-index-channel-bit-analysis"], "default": "disabled",
            "help": "OPT-IN ring reply channel bits in RCHNUM, cleared by ACK; no CPU interrupt injection",
        },
        "bsi_scheduler": {
            "type": str, "choices": ["disabled", "mt6768-enable-capture-analysis"], "default": "disabled",
            "help": "OPT-IN UNVERIFIED BSI scheduler write capture; no inferred enable policy, readback, dispatch or IRQs",
        },
        "mipi": {
            "type": str, "choices": ["disabled", "mt6768-init-capture-analysis"], "default": "disabled",
            "help": "OPT-IN UNVERIFIED MIPI initializer write capture; no reads, transactions or IRQ effects",
        },
        "d2bif": {
            "type": str, "choices": ["disabled", "93xx-storage-analysis"], "default": "disabled",
            "help": "OPT-IN UNVERIFIED D2BIF two-word storage hypothesis; no DMA or IRQ effects",
        },
        "lte_timer": {
            "type": str, "choices": ["disabled", "93xx-rr-config", "93xx-control", "93xx-init-storage-analysis", "93xx-group-cancel-analysis", "93xx-event-cancel-analysis"], "default": "disabled",
            "help": "OPT-IN LTE profiles; analysis variants have UNVERIFIED init storage and optional queued-event cancellation, not a running timer",
        },
        "ccci_mailbox_profile": {
            "type": PurePath, "default": None,
            "help": "OPT-IN reviewed mailbox services with synthetic AP state; no real AP peer",
        },
        "ccci_closed_ports": {
            "type": PurePath, "default": None,
            "help": "OPT-IN explicit unopened AP character-port profile; no application replies",
        },
        "ccci_ipc": {
            "type": str, "choices": ["disabled", "wmt-unavailable"], "default": "disabled",
            "help": "OPT-IN analysis WMT/STP-unavailable sink; no application reply or real AP peer",
        },
        "idc_control": {
            "type": str, "choices": ["disabled", "mt6768-counter"], "default": "disabled",
            "help": "OPT-IN IDC TX-counter enable with idle scheduler; no event completions or peer",
        },
        "idc_uart": {
            "type": str, "choices": ["disabled", "mt6768-control"], "default": "disabled",
            "help": "OPT-IN IDC UART configuration; no pattern matching, peer, DMA or guest IRQ routing",
        },
        "bsi": {
            "type": str, "choices": ["disabled", "mt6768-observe", "mt6768-pending", "mt6768-capture-writes", "mt6768-software-rf"], "default": "disabled",
            "help": "OPT-IN BSI analysis; capture-writes substitutes a write sink, never RF read values",
        },
        "rf_analysis_profile": {
            "type": PurePath, "default": None,
            "help": "ROM-bound explicit SOFTWARE RF identity/ECO assumptions; requires mt6768-software-rf",
        },
        "pmic_analysis_profile": {
            "type": PurePath, "default": None,
            "help": "OPT-IN ROM-bound shared PMIC storage analysis; native pending/software-RF BSI only",
        },
        "abbmix_analysis_profile": {
            "type": PurePath, "default": None,
            "help": "OPT-IN ROM-bound SOFTWARE ABB calibration results; no analog fidelity; native only",
        },
        "mml2_mmu": {
            "type": str, "choices": ["disabled", "93xx-control"], "default": "disabled",
            "help": "OPT-IN reviewed 93xx MML2 MCU MMU control ABI; native analysis only, no translation/DMA",
        },
        "cpu_topology": {
            "type": PurePath, "default": None,
            "help": "Experimental ROM-bound native MIPS MT topology profile; requires matching development PANDA",
        },
        "cpu_model": {
            "type": str, "choices": ["24Kc", "cockpit-mtk-legacy"], "default": "24Kc",
            "help": "24Kc for the pinned legacy engine; cockpit-mtk-legacy requires the isolated-model development engine",
        },
        "boot_mode": {
            "type": str, "choices": ["rehosted", "native"], "default": "rehosted",
            "help": "rehosted requires validated startup hooks; native attempts unpatched diagnostic execution",
        },
        "debug_info": {
            "type": str, "choices": ["auto", "ignore"], "default": "auto",
            "help": "ignore withholds debug information for stripped-image regression tests",
        },
        "symbol_profile": {
            "type": PurePath, "default": None,
            "help": "Reviewed ROM-hash-bound symbol locations/signatures (JSON)",
        },
        "observe_ram": {
            "type": PurePath, "default": None,
            "help": "Native diagnostics: ROM-hash-bound JSON list of RAM words to observe (no MMIO)",
        },
        "sej_analysis_state": {
            "type": PurePath, "default": None,
            "help": "OPT-IN synthetic SEJ identity file (0600); native analysis only, NOT vendor HUK/attestation",
        },
        "nv_data": {
            "type": PurePath,
            "help": "A path to MTK vendor data directory",
            "default": "./mnt",
        },
    }

    @property
    def ARCH(self):
        return MIPS_LE

    @staticmethod
    def is_relevant(path):
        ft = filetype.guess(path)
        return (ft is not None and ft.mime in ["application/x-tar"]) or path.endswith(
            ".img"
        )

    def try_load(self):
        try:
            self.md1img = self.unpack_md1img(self.path)
        except MDFileException as e:
            log.error("%s", e)
            return False

        self.sections = {s.name: s for s in self.iter_section_info()}

        if MAIN_IMG_NAME not in self.sections or "md1dsp" not in self.sections:
            log.error("MTK image requires md1rom and md1dsp, not debug information")
            return False
        try:
            dbg_info = {} if self.loader_args["debug_info"] == "ignore" else self.parse_debug_info()
        except (ValueError, EOFError, struct.error, lzma.LZMAError) as exc:
            log.error("Malformed optional debug information: %s", exc)
            return False

        if not self.guess_soc_version():
            return False

        self.boot_mode = self.loader_args["boot_mode"]
        rom = self.rom_img_data()
        if not rom or len(rom) > 0x2000000:
            log.error("ROM does not fit the current MTK platform's 32-MiB ROM window")
            return False
        resolver = Resolver(rom, 0x90000000 + ROM_BASE_ADDR, self.modem_soc.name, dbg_info)
        try:
            profile = self.loader_args["symbol_profile"]
            if profile:
                with open(profile) as source:
                    resolver.apply_profile(json.load(source))
            resolver.scoped_patterns(PATTERNS)
        except (ValueError, OSError, TypeError) as exc:
            self.capability_report = resolver.capabilities(self.boot_mode)
            self.capability_report["profile_error"] = str(exc)
            self.capability_report["startup_locations_ready"] = False
            self.write_capability_report()
            log.error("Symbol profile rejected: %s", exc)
            return False
        self.symbols, self.symbol_sizes = resolver.symbols, resolver.sizes
        self.capability_report = resolver.capabilities(self.boot_mode)
        self.capability_report["debug_info_policy"] = self.loader_args["debug_info"]
        self.write_capability_report()
        if not self.capability_report["startup_locations_ready"]:
            missing = sorted({name for g in self.capability_report["capabilities"].values()
                              if g["required"] for name in g["missing"]})
            log.error("Rehosted startup requires unresolved locations: %s. See capabilities.json. "
                      "Use explicit native mode for unpatched diagnostic execution.", ", ".join(missing))
            return False

        if not self.build_memory_map():
            return False

        log.info("Loaded MTK image with %d sections", len(self.sections))

        parsed_lted = None
        if self.boot_mode == "native":
            log.warning("Native diagnostic mode: no symbol-based startup patches, task surgery or named trace hooks")
        elif not self.workspace.path("/ltedb.pickle").exists():

            log.info("Parsing MTK debug database...")

            md1_mddb = self.sections.get(DBG_DB_NAME)

            # parsing of trace files is implemented on files, hence we use BytesIO here
            if md1_mddb is not None:
                md1_mddb_segments = readCATD(BytesIO(md1_mddb.data))
                lteds = [x for x in md1_mddb_segments if x[:4] == b"LTED"]

                # MTK machines heavily rely on having debug info. This is fatal
                if len(lteds) == 0:
                    log.warning("Failed to parse MTK debug database or missing 'LTED'")
                else:
                    if len(lteds) > 1:
                        log.warning("More than one LTED entry! Choosing first one")

                    log.info("Parsing LTE DB...")
                    parsed_lted = readLTED(BytesIO(lteds[0]))

                    log.info("Caching DB to workspace...")
                    with open(
                        self.workspace.path("/ltedb.pickle").to_path(), "wb"
                    ) as f:
                        pickle.dump(parsed_lted, f)
        else:
            log.info("Loading cached MTK debug database...")

            with open(self.workspace.path("/ltedb.pickle").to_path(), "rb") as f:
                parsed_lted = pickle.load(f)

        if parsed_lted is None:
            log.warning("Missing LTE trace strings - debug output will suffer")
            self.trace_entries = {}
        else:
            log.info("Loaded database with %d trace entries", len(parsed_lted["trace"]))
            self.trace_entries = parsed_lted["trace"]

        # Check to see if NV data is available
        nv_data_path = self.loader_args["nv_data"]
        if not os.path.isdir(nv_data_path):
            log.error("NV data %s directory is missing", nv_data_path)
            return False

        sub_path = nv_data_path / "vendor" / "nvdata"

        if not os.path.isdir(sub_path):
            log.error(
                "NV data %s directory is available, but missing %s subfolder",
                nv_data_path,
                sub_path,
            )
            return False

        if len(os.listdir(sub_path)) == 0:
            log.warning("NV data directory looks empty. Modem will try to recover and create defaults...")

        log.info("Using NV data from %s", nv_data_path)

        return True

    def write_capability_report(self):
        pending = self.workspace.path("/capabilities.json.tmp").to_path()
        with open(pending, "w") as output:
            json.dump(self.capability_report, output, indent=2)
        os.replace(pending, self.workspace.path("/capabilities.json").to_path())

    def unpack_md1img(self, infile):
        while True:
            g = filetype.guess(infile)
            if g is not None and g.mime == "application/x-tar":
                tar = TarFile(infile)
                name = None
                for n in tar.getnames():
                    if "md1img" in n:
                        name = n
                if name is None:
                    raise MDFileException("md1img not found!")
                tar.extract(name, set_attrs=False)
                if name.endswith("lz4"):
                    unl4 = lz4.frame.open(name)
                    name = name[:-4]
                    with open(name, "wb") as f:
                        f.write(unl4.read())
                infile = name

            elif g is None and ".img" in infile:
                return infile
            else:
                raise MDFileException(f"Could not handle {infile} of type {g.mime}")

    def _getstr(self, raw):
        out = bytearray()
        while True:
            c = raw.read(1)
            if not c:
                raise EOFError("unterminated debug-info string")
            if c == b"\x00":
                break
            out += c
        return out.decode()

    def rom_img_data(self):
        return self.sections[MAIN_IMG_NAME].data

    def parse_debug_info(self):
        if DBG_INFO_NAME not in self.sections:
            log.info("No %s; continuing without vendor function symbols", DBG_INFO_NAME)
            return {}

        log.info("Parsing debug info...")

        debug_compressed = self.sections[DBG_INFO_NAME].data
        decompressor = lzma.LZMADecompressor()
        debug_data = BytesIO(decompressor.decompress(debug_compressed))

        debug_info = {}

        # parse header
        debug_data.seek(0x1C)
        target = self._getstr(debug_data)
        hwplatform = self._getstr(debug_data)
        moly_version = self._getstr(debug_data)
        buildtime = self._getstr(debug_data)

        fn_syms_off = struct.unpack("<I", debug_data.read(4))[0] + 0x10
        file_syms_off = struct.unpack("<I", debug_data.read(4))[0] + 0x10

        while True:
            name = self._getstr(debug_data)
            start = struct.unpack("<I", debug_data.read(4))[0]
            end = struct.unpack("<I", debug_data.read(4))[0]

            while name in debug_info:
                name = name + "_"
            debug_info[name] = (start, end - start)

            if debug_data.tell() >= file_syms_off:
                break
        return debug_info

    def debug_info_to_csv(self, csvfile):
        """
        The CSV format follows the polypyus format
        """
        dbg_info = self.parse_debug_info()
        with open(csvfile, "w") as file:
            fieldnames = ["name", "addr", "size", "mode", "type"]
            writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=" ")

            writer.writeheader()
            for name, addrs in dbg_info.items():
                writer.writerow(
                    {
                        "name": name,
                        "addr": addrs[0],
                        "size": addrs[1],
                        "mode": "UNKOWN",
                        "type": "FUNC",
                    }
                )

    def debug_info_from_csv(self, csvfile):
        """
        The CSV format follows the polypyus format
        """
        dbg_info = {}
        with open(csvfile, "r") as file:
            fieldnames = ["name", "addr", "size", "mode", "type"]
            reader = csv.DictReader(file, delimiter=" ")
            for row in reader:
                dbg_info[row["name"]] = (row["addr"], row["size"])

        return dbg_info

    def iter_section_info(self):
        off = 0
        file_length = stat(self.md1img).st_size

        with open(self.md1img, "rb") as f:
            while off < file_length:
                f.seek(off)
                header = f.read(0x50)

                # special case for samsung signatures
                if header[:9] == b"SignerVer":
                    return
                contents = struct.unpack("<II32sIIIIIIIIII", header)

                magic = contents[0]
                length = contents[1]
                name = contents[2][
                    : contents[2].find(b"\x00")
                ].decode()  # strip after 0byte
                maddr = contents[3]
                mode = contents[4]
                magic2 = contents[5]
                data_off = contents[6]

                log.info(
                    "Found new file {:s} at 0x{:x}/0x{:x} with length 0x{:x}".format(
                        name, off, maddr, length
                    )
                )

                assert (
                    magic == MAGIC and magic2 == MAGIC2
                )  # either EOF, or we did smthg wrong

                yield MTKSection(self, name, length, maddr, mode, off, off + data_off)

                off = off + data_off + length
                if off % 0x10:
                    off = off - off % 0x10 + 0x10

    def guess_soc_version(self):

        # The DSP is closely tied to the hardware platform and includes a build time
        dsp_data = self.sections["md1dsp"].data

        # Try to find the version with the date first
        found = re.search(
            rb"""
        (?P<date>[0-9]{4}/[0-9]{2}/[0-9]{2}?)  # Date as YYYY/MM/DD (for rough SoC revision)
        (?P<time>[ ][0-9]{2}:[0-9]{2})?   # time in HH:MM format
        .{,100}                 # variable sized inbetween-data
        (?P<SOC>MT[0-9]{4}?) # SOC-ID
        [^\x00]*               # null terminator""",
            dsp_data,
            re.M | re.S | re.X,
        )

        if found is None:
            log.error(
                "Unable to automatically determine the SoC type from the boot image"
            )
            return False

        soc_guess = found.group("SOC").decode()

        # SoC date is a best effort approach
        soc_date = found.group("date").decode() if "date" in found.groupdict() else 0

        self.modem_soc = get_soc(self.NAME, soc_guess)

        if self.modem_soc is None:
            log.error("Guessed SoC '%s' is not supported", soc_guess)
            return False

        # Initialize SoC object
        self.modem_soc = self.modem_soc(soc_date)

        log.info("SoC %s (automatic)", repr(self.modem_soc))

        self._machine_class = MT6878Machine

        return True

    def build_memory_map(self):
        notifications = self.loader_args.get("ccif_notifications", "disabled")
        if notifications not in ("disabled", "ring-index-channel-bit-analysis"):
            raise ValueError("Unsupported CCIF reply notification ABI")
        if notifications != "disabled":
            if self.boot_mode != "native":
                raise ValueError("CCIF notifications require native boot mode")
            targets = [p for p in self.modem_soc.peripherals
                       if issubclass(p._cls, PCCIF_Periph) and p._attr.get("pccifid") == 0]
            if len(targets) != 1:
                raise ValueError("CCIF notifications require exactly one ring transport")
        ipc_mode = self.loader_args.get("ccci_ipc", "disabled")
        if ipc_mode not in ("disabled", "wmt-unavailable"):
            raise ValueError("Unsupported CCCI IPC policy")
        if ipc_mode != "disabled" and self.boot_mode != "native":
            raise ValueError("CCCI IPC analysis requires native boot mode")
        ports_path = self.loader_args.get("ccci_closed_ports")
        ports_profile = None
        if ports_path is not None:
            if self.boot_mode != "native":
                raise ValueError("Closed AP ports require native boot mode")
            with open(ports_path) as source:
                ports_profile = json.load(source)
            ClosedAPPorts(ports_profile)  # Validate before creating any mappings.
        mailbox_path = self.loader_args.get("ccci_mailbox_profile")
        mailbox_dispatcher = mailbox_facts = None
        if mailbox_path is not None:
            if self.boot_mode != "native":
                raise ValueError("Mailbox analysis requires native boot mode")
            with open(mailbox_path, "rb") as source:
                raw = source.read()
            mailbox_dispatcher, mailbox_facts = build_mailbox_profile(json.loads(raw))
            mailbox_facts["profile_sha256"] = hashlib.sha256(raw).hexdigest()
            if ports_profile is not None:
                mailbox_dispatcher.validate_channels(ClosedAPPorts(ports_profile).channels)
            targets = [p for p in self.modem_soc.peripherals
                       if issubclass(p._cls, PCCIF_Periph) and p._attr.get("pccifid") == 0]
            if len(targets) != 1:
                raise ValueError("Mailbox profile requires exactly one PCCIF0 transport")
        self.build_peripheral_maps()

        ########################
        # Peripheral Memory Map
        ########################

        for peripheral in self.modem_soc.peripherals:
            if peripheral is getattr(self, "pmic_analysis_wrapper", None):
                attributes = dict(peripheral._attr)
                attributes["pmic_target"] = self.pmic_analysis_binding.target
                self.create_peripheral(peripheral, peripheral._address, peripheral._size, **attributes)
            elif ((notifications != "disabled" or ipc_mode != "disabled" or ports_profile is not None or mailbox_dispatcher is not None) and issubclass(peripheral._cls, PCCIF_Periph)
                    and peripheral._attr.get("pccifid") == 0):
                attributes = dict(peripheral._attr)
                if notifications != "disabled":
                    attributes["reply_notifications"] = notifications
                    self.capability_report["ccif_notifications"] = dict(abi=notifications,
                        analysis_only=True, cpu_interrupt_connected=False, application_delivery_verified=False)
                if ipc_mode != "disabled":
                    attributes["ipc_dispatcher"] = unavailable_wmt_dispatcher()
                    self.capability_report["ccci_ipc"] = {
                        "policy": "wmt-stp-unavailable-analysis/v1", "abi": "ccci-ipc-ilm32/v1",
                        "peer_connected": False, "application_verified": False,
                        "response_supported": False,
                    }
                if ports_profile is not None:
                    attributes["closed_ports"] = ClosedAPPorts(ports_profile)
                    self.capability_report["ccci_closed_ports"] = attributes["closed_ports"].facts()
                if mailbox_dispatcher is not None:
                    attributes["mailbox_dispatcher"] = mailbox_dispatcher
                    self.capability_report["ccci_mailbox"] = mailbox_facts
                self.create_peripheral(peripheral, peripheral._address, peripheral._size, **attributes)
                self.write_capability_report()
            else:
                self.create_soc_peripheral(peripheral)

        return True

    def build_peripheral_maps(self):
        abbmix = None
        abbmix_path = self.loader_args.get("abbmix_analysis_profile")
        if abbmix_path is not None:
            from .abbmix_profile import load_abbmix_profile
            from .hw.AbbMixPeripheral import AbbMixAnalysisPeripheral
            abbmix = load_abbmix_profile(abbmix_path, self.capability_report["rom_sha256"],
                                        boot_mode=self.boot_mode)
            profile, calibration, profile_sha = abbmix
            if (profile["base"] % 0x1000 or profile["size"] % 0x1000
                    or not 0xA6190000 <= profile["base"] < profile["base"] + profile["size"] <= 0xA619E000):
                raise ValueError("ABB analysis profile must fit page-aligned inside the existing ABBMIX window")
            self.capability_report["abbmix_analysis"] = dict(
                calibration.facts(), profile_sha256=profile_sha,
                physical_base=profile["base"], size=profile["size"], profile=profile)
        self.pmic_analysis_binding = self.pmic_analysis_wrapper = None
        pmic_path = self.loader_args.get("pmic_analysis_profile")
        if pmic_path is not None:
            from .pmic_profile import load_pmic_analysis, select_wrapper
            self.pmic_analysis_binding = load_pmic_analysis(pmic_path,
                self.capability_report["rom_sha256"], boot_mode=self.boot_mode,
                bsi_mode=self.loader_args.get("bsi", "disabled"))
            self.pmic_analysis_wrapper = select_wrapper(self.pmic_analysis_binding, self.modem_soc.peripherals)
        scheduler_abi = self.loader_args.get("bsi_scheduler", "disabled")
        if scheduler_abi not in ("disabled", "mt6768-enable-capture-analysis"):
            raise ValueError("Unsupported BSI scheduler ABI")
        if scheduler_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("BSI scheduler analysis requires native boot mode")
            self.add_memory_range(0xA6150000, 0x1000, name="BSI_SCHEDULER",
                                  emulate=MTKBsiSchedulerCapturePeripheral, permissions="rw-")
            self.capability_report["bsi_scheduler"] = dict(
                MTKBsiSchedulerCapturePeripheral.analysis_facts(),
                abi=scheduler_abi, physical_base=0xA6150000, size=0x1000)
            self.write_capability_report()
        mipi_abi = self.loader_args.get("mipi", "disabled")
        if mipi_abi not in ("disabled", "mt6768-init-capture-analysis"):
            raise ValueError("Unsupported MIPI ABI")
        if mipi_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("MIPI initialization capture requires native boot mode")
            self.add_memory_range(0xA6173000, 0x5000, name="MIPI_INIT_CAPTURE",
                                  emulate=MTKMipiInitCapturePeripheral, permissions="rw-")
            self.capability_report["mipi"] = dict(
                MTKMipiInitCapturePeripheral.analysis_facts(),
                abi=mipi_abi, physical_base=0xA6173000, size=0x5000)
            self.write_capability_report()
        d2bif_abi = self.loader_args.get("d2bif", "disabled")
        if d2bif_abi not in ("disabled", "93xx-storage-analysis"):
            raise ValueError("Unsupported D2BIF ABI")
        if d2bif_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("D2BIF storage analysis requires native boot mode")
            self.add_memory_range(0xAB820000, 0x1000, name="D2BIF",
                                  emulate=MTKD2BIFStorageAnalysisPeripheral, permissions="rw-")
            self.capability_report["d2bif"] = dict(
                MTKD2BIFStorageAnalysisPeripheral.analysis_facts(),
                abi=d2bif_abi, physical_base=0xAB820000, size=0x1000)
            self.write_capability_report()
        lte_abi = self.loader_args.get("lte_timer", "disabled")
        lte_classes = {"93xx-rr-config": MTKLTETimerRRPeripheral,
                       "93xx-control": MTKLTETimerControlPeripheral,
                       "93xx-init-storage-analysis": MTKLTETimerInitStorageAnalysisPeripheral,
                       "93xx-group-cancel-analysis": MTKLTETimerGroupCancelAnalysisPeripheral,
                       "93xx-event-cancel-analysis": MTKLTETimerEventCancelAnalysisPeripheral}
        if lte_abi != "disabled" and lte_abi not in lte_classes:
            raise ValueError("Unsupported LTE timer ABI")
        if lte_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("LTE timer configuration analysis requires native boot mode")
            lte_cls = lte_classes[lte_abi]
            self.add_memory_range(0xA6090000, 0x2000, name="LTE_TIMER",
                                  emulate=lte_cls, permissions="rw-")
            self.capability_report["lte_timer"] = {
                "abi": lte_abi, "physical_base": 0xA6090000,
                "configuration_only": True, "clock_supported": False,
                "rr_trigger_supported": False, "guest_irq_routed": False,
            }
            if issubclass(lte_cls, MTKLTETimerInitStorageAnalysisPeripheral):
                self.capability_report["lte_timer"].update(lte_cls.analysis_facts())
            self.write_capability_report()
        control_abi = self.loader_args.get("idc_control", "disabled")
        if control_abi not in ("disabled", "mt6768-counter"):
            raise ValueError("Unsupported IDC control ABI")
        if control_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("IDC control analysis requires native boot mode")
            self.add_memory_range(0xA60A0000, 0x1000, name="IDC_CTRL",
                                  emulate=MTKIDCControlPeripheral, permissions="rw-")
            self.capability_report["idc_control"] = {
                "abi": control_abi, "physical_base": 0xA60A0000,
                "scheduler_supported": False, "peer_connected": False,
            }
            self.write_capability_report()
        idc_abi = self.loader_args.get("idc_uart", "disabled")
        if idc_abi not in ("disabled", "mt6768-control"):
            raise ValueError("Unsupported IDC UART ABI")
        if idc_abi != "disabled":
            if self.boot_mode != "native":
                raise ValueError("IDC UART control analysis requires native boot mode")
            self.add_memory_range(0xA60B0000, 0x1000, name="IDC_UART",
                                  emulate=MTKIDCUARTPeripheral, permissions="rw-")
            self.capability_report["idc_uart"] = {
                "abi": idc_abi, "physical_base": 0xA60B0000,
                "peer_connected": False, "guest_irq_routed": False,
                "timing_verified": False, "control_only": True,
                "pattern_configuration_supported": True, "pattern_matching_supported": False,
            }
            self.write_capability_report()
        bsi_mode = self.loader_args.get("bsi", "disabled")
        if bsi_mode not in ("disabled", "mt6768-observe", "mt6768-pending", "mt6768-capture-writes", "mt6768-software-rf"):
            raise ValueError("Unsupported BSI ABI")
        if bsi_mode != "disabled" and self.boot_mode != "native":
            raise ValueError("BSI analysis requires native boot mode")
        rf_path = self.loader_args.get("rf_analysis_profile")
        if (bsi_mode == "mt6768-software-rf") != (rf_path is not None):
            raise ValueError("Software RF requires explicit mode AND profile")
        rf_profile = None
        if rf_path is not None:
            from .hw.rf_serial import validate_software_rf_profile
            with open(rf_path) as source:
                rf_profile = validate_software_rf_profile(json.load(source), self.capability_report["rom_sha256"])
            self.capability_report["software_rf_analysis"] = rf_profile
            self.write_capability_report()
        mmu_abi = self.loader_args.get("mml2_mmu", "disabled")
        pmic_kwargs = {}
        if self.pmic_analysis_binding is not None:
            binding = self.pmic_analysis_binding
            if rf_profile is not None and str(binding.bsi_port) in (
                    set(rf_profile["ports"]) | set(rf_profile.get("idle_mipi_ports", {}))):
                raise ValueError("PMIC serial port conflicts with another configured target")
            pmic_kwargs = {"pmic_target": binding.target, "pmic_port": binding.bsi_port}
            self.capability_report["pmic_analysis"] = binding.facts()
            self.write_capability_report()
        if mmu_abi not in ("disabled", "93xx-control"):
            raise ValueError("Unsupported MML2 MMU ABI")
        if mmu_abi != "disabled" and self.boot_mode != "native":
            raise ValueError("MML2 control-only analysis requires native boot mode")
        # peripherals
        self.add_memory_range(
            0x1F000000, 0x8000, name="GCR", emulate=GCR_Periph, permissions="rw-"
        )
        self.add_memory_range(
            0x1FC00000, 0x1000, name="MDMCU_MDMCU", permissions="rw-"
        )  # ITC?
        self.add_memory_range(
            0x1FC10000, 0x1000, name="CDMM", emulate=CDMM_Periph, permissions="rw-"
        )
        # 0x1f020000 GIC
        # 0x1f008000 CPC

        # self.add_memory_range(0x1f010000, 0x8000, name='GCRCustom', emulate=GCRCustom_Periph, permissions='rw-')
        self.add_memory_range(
            0x1F010000,
            0x1000,
            name="GCRCustom",
            emulate=GCRCustom_Periph,
            permissions="rw-",
        )
        self.add_memory_range(0x1F014000, 0x1000, name="GCR_MDCIRQ", permissions="rw-")
        self.add_memory_range(0x1F01C000, 0x1000, name="ULSP_PB", permissions="rw-")
        self.add_memory_range(
            0xA0000000,
            0x1000,
            name="MDPERI_MDCFGCTL",
            emulate=MDCFGCTL_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA0020000, 0x1000, name="MDPERI_MDGDMA", permissions="rw-"
        )
        self.add_memory_range(
            0xA0030000, 0x1000, name="MDPERI_MDGPTM", permissions="rw-"
        )
        self.add_memory_range(
            0xA0060000,
            0x2000,
            name="MDPERI_MDPERISYS_MISC_REG",
            emulate=MDPERISYS_MISC_Periph,
            permissions="rw-",
        )
        # avatar.add_memory_range(0xa0061000, 0x1000, name='MDPERI_MDPERISYS_MISC_REG_cont', emulate=PassthroughPeripheral, permissions='rw-')
        self.add_memory_range(
            0xA0070000,
            0x1000,
            name="MDPERI_MDCIRQ",
            emulate=MDCIRQ_Periph,
            permissions="rw-",
        )
        # TODO: is this all DBGSYS1? maybe..
        self.add_memory_range(
            0xA0080000, 0x10000, name="MDPERI_MD_DBGSYS1", permissions="rw-"
        )
        self.add_memory_range(
            0xA00C0000, 0x1000, name="MDPERI_PTP_THERM_CTRL", permissions="rw-"
        )
        # TODO: F32K_CNT is accessed so much that this is a perf issue (MD_TOPSM)
        # avatar.add_memory_range(0xa00d0000, 0x1000, name='MDPERI_MD_TOPSM', emulate=TOPSM_Periph, permissions='rw-')
        self.add_memory_range(
            0xA00E0000,
            0x1000,
            name="MDPERI_MD_OSTIMER",
            emulate=OSTimer_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA00F0000, 0x1000, name="MDPERI_MDRGU", permissions="rw-"
        )
        self.add_memory_range(
            0xA0100000, 0x1000, name="MDPERI_MDSM_CORE_PWR_CTRL", permissions="rw-"
        )
        # note that MDPERI_MD_EINT starts at EINT_ADDR_OFFSET = 0x1000, so I added a _GPIOMUX for the base
        self.add_memory_range(
            0xA0110000, 0x2000, name="MDPERI_MD_EINT_GPIOMUX", permissions="rw-"
        )
        self.add_memory_range(
            0xA0130000, 0x1000, name="MDPERI_MD_GLOBAL_CON_DCM", permissions="rw-"
        )
        self.add_memory_range(
            0xA0140000, 0x1000, name="MDPERI_MD_PLLMIXED", permissions="rw-"
        )
        self.add_memory_range(
            0xA0150000,
            0x1000,
            name="MDPERI_MD_CLKSW",
            emulate=CLKSW_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA01C0000, 0x1000, name="MDPERI_CLK_CTRL", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D2000, 0x1000, name="MDPERI_CORE0_MEM_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D3000, 0x1000, name="MDPERI_CORE1_MEM_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D4000, 0x1000, name="MDPERI_MDCORE_MEM_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D5000, 0x1000, name="MDPERI_MDINFRA_MEM_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D6000, 0x1000, name="MDPERI_MDMEMSLP_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA01D8000, 0x1000, name="MDPERI_MDPERISYS_MEM_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA0210000, 0x1000, name="MDMCU_IA_PDA_MON", permissions="rw-"
        )
        self.add_memory_range(
            0xA0300000, 0x1000, name="MDCORESYS_MML2_MCU_MMU_MMU", permissions="rw-",
            **({"emulate": MML2MMU93Peripheral} if mmu_abi == "93xx-control" else {})
        )
        self.add_memory_range(
            0xA0301000, 0x1000, name="MDCORESYS_MML2_MCU_MMU_VRB", permissions="rw-"
        )
        self.add_memory_range(
            0xA0302000, 0x1000, name="MDCORESYS_MML2_MCU_MMU", permissions="rw-"
        )
        self.add_memory_range(
            0xA0310000, 0x1000, name="MDMCU_BUSMON", permissions="rw-"
        )
        self.add_memory_range(
            0xA0330000, 0x1000, name="MDMCU_BUS_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA0350000, 0x1000, name="MDCORESYS_MDMCU_ELM_EMI", permissions="rw-"
        )
        self.add_memory_range(
            0xA0360000, 0x1000, name="MDCORESYS_MISC_REG_ADR_IF", permissions="rw-"
        )
        # this is MCUSYS_MISC_REG + 0x10000
        self.add_memory_range(
            0xA0370000, 0x1000, name="BUSMPU_ERR_REG", permissions="rw-"
        )
        self.add_memory_range(
            0xA04A0000, 0x1000, name="MDINFRA_MDSMICFG", permissions="rw-"
        )
        self.add_memory_range(0xA04F0000, 0x1000, name="MDINFRA_LOG", permissions="rw-")
        self.add_memory_range(
            0xA0520000, 0x1000, name="MDINFRA_MD_INFRA_ELM", permissions="rw-"
        )
        self.add_memory_range(
            0xA0560000, 0x1000, name="MDINFRA_PPPHA", permissions="rw-"
        )
        self.add_memory_range(
            0xA0600000, 0x1000, name="MML2_QUEUE_PROCESSOR", permissions="rw-"
        )
        self.add_memory_range(0xA060B000, 0x1000, name="MML2_CFG", permissions="rw-")
        self.add_memory_range(
            0xA1000000, 0x40000, name="USIP_USIP0_ITCM", permissions="rw-"
        )
        self.add_memory_range(
            0xA1040000, 0x40000, name="USIP_USIP0_DTCM", permissions="rw-"
        )
        self.add_memory_range(
            0xA10A0000, 0x1000, name="USIP_USIP0_power", permissions="rw-"
        )
        self.add_memory_range(
            0xA1100000, 0x40000, name="USIP_USIP1_ITCM", permissions="rw-"
        )
        self.add_memory_range(
            0xA1140000, 0x40000, name="USIP_USIP1_DTCM", permissions="rw-"
        )
        self.add_memory_range(
            0xA11A0000, 0x1000, name="USIP_USIP1_power", permissions="rw-"
        )
        self.add_memory_range(0xA1600000, 0x1000, name="USIP_CONFG", permissions="rw-")
        self.add_memory_range(
            0xA1630000, 0x1000, name="USIP_CROSS_CORE_CTRL", permissions="rw-"
        )
        self.add_memory_range(
            0xA1FF0000,
            0x1000,
            name="MDMCU_MCU_SYNC",
            emulate=MCUSync_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA6000000,
            0x1000,
            name="MODEML1_AO_MODEML1_TOPSM",
            emulate=MODEML1_TOPSM_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA6010000, 0x1000, name="MODEML1_AO_MODEML1_DVFS_CTRL", permissions="rw-"
        )
        self.add_memory_range(
            0xA6020000, 0x1000, name="MODEML1_AO_MODEML1_AO_CONFG", permissions="rw-"
        )
        self.add_memory_range(
            0xA6030000, 0x1000, name="MODEML1_AO_TDMA_SLP", permissions="rw-"
        )
        self.add_memory_range(
            0xA6060000, 0x1000, name="MODEML1_AO_FDD_SLP", permissions="rw-"
        )
        self.add_memory_range(
            0xA6080000, 0x1000, name="MODEML1_AO_LTE_SLP", permissions="rw-"
        )
        self.add_memory_range(
            0xA60C0000, 0x1000, name="MODEML1_AO_C2K_1X_TIMER", permissions="rw-"
        )
        self.add_memory_range(
            0xA60E0000, 0x1000, name="MODEML1_AO_C2K_DO_TIMER", permissions="rw-"
        )
        self.add_memory_range(
            0xA60D0000, 0x1000, name="MODEML1_AO_C2K_1X_SLP", permissions="rw-"
        )
        self.add_memory_range(
            0xA60F0000, 0x1000, name="MODEML1_AO_C2K_DO_SLP", permissions="rw-"
        )

        self.add_memory_range(
            0xA6100000,
            0xD000,
            name="BASE_MADDR_MODEML1_AO_WCT_P2P_TX_PARALLEL",
            permissions="rw-",
        )
        self.add_memory_range(
            0xA6110000, 0x1000, name="MODEML1_AO_FESYS_P2P_TX", permissions="rw-"
        )
        self.add_memory_range(
            0xA6120000, 0x1000, name="MODEML1_AO_MDRX_P2P_TX", permissions="rw-"
        )
        self.add_memory_range(
            0xA6140000, 0x1000, name="BASE_MADDR_MODEML1_AO_BSI_MM", permissions="rw-"
        )
        self.add_memory_range(
            0xA6160000, 0x9000, name="MODEML1_AO_BSI_MM_2", permissions="rw-",
            **({"emulate": BSIImmediatePeripheral, "bsi_mode": bsi_mode.split("-", 1)[1], "rf_profile": rf_profile, **pmic_kwargs}
               if bsi_mode != "disabled" else {})
        )
        self.add_memory_range(
            0xA6170000, 0x3000, name="MODEML1_AO_BSI_MM_3", permissions="rw-"
        )
        self.add_memory_range(
            0xA6180000, 0x3000, name="BASE_MADDR_MODEML1_AO_BPI_MM", permissions="rw-"
        )
        if abbmix is None:
            self.add_memory_range(0xA6190000, 0xE000,
                name="BASE_MADDR_MODEML1_AO_ABBMIX_PKR_P2P_TX", permissions="rw-")
        else:
            profile, calibration, profile_sha = abbmix
            start, end = profile["base"], profile["base"] + profile["size"]
            if start > 0xA6190000:
                self.add_memory_range(0xA6190000, start - 0xA6190000,
                    name="ABBMIX_PREFIX", permissions="rw-")
            self.add_memory_range(start, profile["size"], name="ABBMIX_CAL",
                emulate=AbbMixAnalysisPeripheral, calibration=calibration,
                modeled_range=profile["modeled_range"], permissions="rw-")
            if end < 0xA619E000:
                self.add_memory_range(end, 0xA619E000 - end, name="ABBMIX_SUFFIX", permissions="rw-")
        self.add_memory_range(
            0xA61A0000, 0x1000, name="BASE_MADDR_MODEML1_AO_C1X_TTR", permissions="rw-"
        )
        self.add_memory_range(
            0xA61B0000, 0x1000, name="BASE_MADDR_MODEML1_AO_CDO_TTR", permissions="rw-"
        )

        self.add_memory_range(
            0xA6F00000, 0x1000, name="MD2GSYS_MD2G_CONFG", permissions="rw-"
        )
        self.add_memory_range(
            0xA6F20000,
            0x1000,
            name="MD2GSYS_TDMA_BASE",
            emulate=TDMABase_Periph,
            permissions="rw-",
        )
        self.add_memory_range(
            0xA6F40000, 0x1000, name="MD2GSYS_BFE_2ND", permissions="rw-"
        )
        self.add_memory_range(0xA6FE0000, 0x1000, name="MD2GSYS_BFE", permissions="rw-")

        self.add_memory_range(
            0xA7010000, 0x1000, name="RXDFESYS_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xA70C0000, 0x9000, name="RXDFESYS_RXDFE_FC", permissions="rw-"
        )
        self.add_memory_range(
            0xA70D0000, 0x1000, name="RXDFESYS_RXDFE_ATIMER", permissions="rw-"
        )
        self.add_memory_range(
            0xA7430000, 0x1000, name="RXDFESYS_RXDFE_FCCALTC_SRAM", permissions="rw-"
        )
        self.add_memory_range(
            0xAC350000, 0x1000, name="RAKESYS_CIRQ", permissions="rw-"
        )
        self.add_memory_range(
            0xAC351000, 0x1000, name="RAKESYS_PERICTRL", permissions="rw-"
        )
        self.add_memory_range(
            0xAC358000, 0x1000, name="RAKESYS_CMIF", permissions="rw-"
        )
        self.add_memory_range(0xC0005000, 0x1000, name="AP_GPIOMUX", permissions="rw-")
        self.add_memory_range(0xC0006000, 0x1000, name="SPM_PCM", permissions="rw-")
        # pmic_wrap_memory_dump in the current fw binary wants 0x61d entries, TODO: investigate
        # avatar.add_memory_range(0xc000d000, 0x1000, name='APMCU_MISC', emulate=PassthroughPeripheral, permissions='rw-') # aka PMIC_WRAP
        # avatar.add_memory_range(0xc000d000, 0x2000, name='APMCU_MISC', emulate=PMIC_WRAP_Periph, permissions='rw-') # aka PMIC_WRAP -> moved to SOC definition
        self.add_memory_range(0xC000F000, 0x1000, name="SPM_VMODEM", permissions="rw-")
        self.add_memory_range(0xC0012000, 0x1000, name="DVFSRC", permissions="rw-")
        self.add_memory_range(
            0xC0219000, 0x1000, name="AP_EMI_CONFIG", permissions="rw-"
        )
        self.add_memory_range(
            0xC021C800, 0x1000, name="AP_CLDMA_TOP_MD", permissions="rw-"
        )

        self.create_peripheral(AES_TOP0_Periph, 0xC0016000, 0x1000, name="AES_TOP0")
        self.create_peripheral(
            SHM_RUNTIME_Periph, 0x69000000, 0x1000, name="SharedMemoryRUNTIME"
        )
        self.create_peripheral(TOPSM_Periph, 0xA00D0000, 0x1000, name="MDPERI_MD_TOPSM")


firmwire.loader.register_loader(MTKLoader)
