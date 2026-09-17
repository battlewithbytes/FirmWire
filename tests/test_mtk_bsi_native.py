"""Firmware-free native MMIO test; opt-in, bounded child process."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory, read_completion=False, capture=False, software=None, rcal=None, ldo=None, clock_trial=None, rx_rc=None, rx_tpd=None):
    from pandare import Panda
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
    root = Path(directory)
    instructions = [0x3c080040, 0x8d021008, 0xac021000, 0x34091234, 0xad091004,
                    0x34090301, 0xad091000, 0x8d021008, 0xac021004, 0x8d021108,
                    0xac021008, 0x3c080060, 0x8d021008, 0xac02100c, 0xd]
    if read_completion:
        instructions = [0x3c080040, 0x34090003, 0xad091000, 0x8d021204, 0xac021000,
                        0x8d02100c, 0xac021004, 0x8d021010, 0xac021008,
                        0x34090001, 0xad091200, 0x8d021204, 0xac02100c,
                        0x3c080060, 0x8d021204, 0xac021010, 0xd]
    if capture:
        instructions = [0x3c080040, 0x34091234, 0xad091004, 0x34090001, 0xad091000,
                        0x8d021008, 0xac021000, 0x34090400, 0xad091004, 0x34090003,
                        0xad091000, 0x8d021008, 0xac021004, 0x8d021204, 0xac021008,
                        0x3c080060, 0x8d021008, 0xac02100c, 0xd]
    if software:
        from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("""
            li $t0, 0x400000
            li $t1, 0x80000
            sw $t1, 0x1004($t0)
            li $t1, 1
            sw $t1, 0x1000($t0)
            li $t1, 0x400
            sw $t1, 0x1004($t0)
            li $t1, 3
            sw $t1, 0x1000($t0)
            lw $v0, 0x100c($t0)
            sw $v0, 0x1000($zero)
            li $t1, 1
            sw $t1, 0x1200($t0)
            li $t1, 0x555
            sw $t1, 0x1004($t0)
            li $t1, 3
            sw $t1, 0x1000($t0)
            lw $v0, 0x100c($t0)
            sw $v0, 0x1010($zero)
            li $t1, 1
            sw $t1, 0x1200($t0)
            li $t1, 4
            sw $t1, 0x4000($t0)
            li $t1, 75
            sw $t1, 0x4020($t0)
            li $t1, 0x30003
            sw $t1, 0x4024($t0)
            li $t6, 0x408000
            li $t1, 0x212345
            sw $t1, 0x18($t6)
            sw $zero, 0x1c($t6)
            li $t1, 1
            sw $t1, 0x4004($t0)
            li $t1, 5
            sw $t1, 0x4004($t0)
            li $t1, 3
            sw $t1, 0x4004($t0)
            li $t7, 0x800000
        wait_por:
            lw $t4, 0x40($t7)
            lw $v0, 0x4008($t0)
            beqz $v0, wait_por
            nop
            sw $v0, 0x1004($zero)
            li $t1, 0x402
            sw $t1, 0x1004($t0)
            li $t1, 3
            sw $t1, 0x1000($t0)
            lw $v0, 0x100c($t0)
            sw $v0, 0x1008($zero)
            li $t1, 1
            sw $t1, 0x1200($t0)
            li $t1, 0x403
            sw $t1, 0x1004($t0)
            li $t1, 3
            sw $t1, 0x1000($t0)
            lw $v0, 0x1008($t0)
            sw $v0, 0x100c($zero)
            break
        """)
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    if rcal:
        # Real guest MMIO, synthetic instructions/values. A read before setup in
        # bank1 must remain pending even after bank0 performs the correct writes.
        asm = ["li $t0, 0x400000", "li $t1, 0x40a", "sw $t1, 0x1104($t0)",
               "li $t1, 3", "sw $t1, 0x1100($t0)", "lw $v0, 0x1108($t0)", "sw $v0, 0x100c($zero)"]
        for word in (0x881c00, 0x881c01, 0x900000, 0xc00000):
            asm += [f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"]
        for index, word in enumerate((0x40a, 0x40b, 0x409)):
            asm += [f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                    "lw $v0, 0x100c($t0)", f"sw $v0, {0x1000+4*index}($zero)",
                    "li $t1, 1", "sw $t1, 0x1200($t0)"]
        asm += ["li $t1, 0x44d", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                "lw $v0, 0x1008($t0)", "sw $v0, 0x1010($zero)", "break"]
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    if ldo is not None:
        asm = ["li $t0, 0x400000"]
        def write(word):
            asm.extend([f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"])
        def read(index, word=0x44d, status=False):
            asm.extend([f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                        f"lw $v0, {0x1008 if status else 0x100c}($t0)", f"sw $v0, {0x1000+4*index}($zero)"])
            if not status: asm.extend(["li $t1, 1", "sw $t1, 0x1200($t0)"])
        write(0xf05800); write(0x4b80000); read(0)
        write(0xf01800); write(0x4c00008); write(0x4b60000); read(1)
        write(0xf00800)
        write(0xf07800); write(0x4b80000); read(2)
        write(0xf03800); write(0x4c00040); write(0x4b40000)
        write(0x4c00000); read(3)  # RX clears the selector before readback
        read(4, 0x5bf, status=True)  # next unsupported stage stays pending
        asm.append("break")
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    if rx_rc is not None:
        asm = ["li $t0, 0x400000", "li $t1, 0x5bf", "sw $t1, 0x1104($t0)",
               "li $t1, 3", "sw $t1, 0x1100($t0)", "lw $v0, 0x1108($t0)", "sw $v0, 0x1000($zero)"]
        for word in (0x001112a0,0x14000000,0x14100000,0x1d302c01,0x001212a8):
            asm += [f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"]
        asm += ["li $t1, 0x5bf", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                "lw $v0, 0x100c($t0)", "sw $v0, 0x1004($zero)",
                "li $t1, 1", "sw $t1, 0x1200($t0)",
                # Guest derives and writes the duplicated trim; not a host table patch.
                "srl $t2, $v0, 14", "andi $t2, $t2, 63", "sll $t2, $t2, 8",
                "or $t2, $t2, $v0", "li $t1, 0x1bf00000", "or $t2, $t2, $t1",
                "sw $t2, 0x1008($zero)", "sw $t2, 0x1004($t0)",
                "li $t1, 1", "sw $t1, 0x1000($t0)",
                "li $t1, 0x5bf", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                "lw $v0, 0x1008($t0)", "sw $v0, 0x100c($zero)",
                "lw $v0, 0x1108($t0)", "sw $v0, 0x1010($zero)", "break"]
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    if rx_tpd is not None:
        asm = ["li $t0, 0x400000", "li $t1, 0x5a7", "sw $t1, 0x1104($t0)",
               "li $t1, 3", "sw $t1, 0x1100($t0)"]  # early bank1 read stays pending
        def write(word):
            asm.extend([f"li $t1, {word}", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"])
        def read(address,index=None):
            asm.extend([f"li $t1, {0x400|address}", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                        "lw $v0, 0x100c($t0)","li $t1, 1", "sw $t1, 0x1200($t0)"])
            if index is not None: asm.append(f"sw $v0, {0x1000+4*index}($zero)")
        for word in (0x14000001,0x14100001,0x14208051,0x144098b1,0x14600880,
                     0x18f03d7a,0x19003d7a,0x1ef00002,0x1f41b780,0x1f51b780,
                     0x08000001,0x08229276,0x083276a9,0x0b304b0e,0x19d007f8,0x19e007f8):
            write(word)
        for address in (469,472):
            read(address)
            asm += ["li $t2, 0xffc00", "and $t2, $t2, $v0", f"li $t1, {(address<<20)|0x96}",
                    "or $t1, $t1, $t2", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"]
        write(0x001212a8); write(0x00600414)
        read(423,0); read(429,1)
        write(0x00600384)
        for address,index in ((469,2),(472,3)):
            read(address)
            asm += ["li $t2, 0xffc00", "and $t2, $t2, $v0", f"li $t1, {address<<20}",
                    "or $t1, $t1, $t2", "sw $t1, 0x1004($t0)", "li $t1, 1", "sw $t1, 0x1000($t0)"]
            read(address,index)
        asm += ["li $t1, 0x5a7", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                "lw $v0, 0x1008($t0)", "sw $v0, 0x1010($zero)", "break"]
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    if clock_trial is not None:
        # Real guest GCR waits, rather than host sleeps or explicit sequencer
        # advance calls. Two different starting phases and payloads are checked.
        pad, payload = clock_trial
        asm = [f"li $t5, {pad+1}", "padding: addiu $t5, $t5, -1", "bnez $t5, padding", "nop",
               "li $t0, 0x400000", "li $t6, 0x408000", "li $t7, 0x800000"]
        for offset,value in ((0x4000,4),(0x4020,380*75),(0x4024,3<<16|3)):
            asm += [f"li $t1, {value}", f"sw $t1, {offset}($t0)"]
        asm += [f"li $t1, {(173<<20)|payload}", "sw $t1, 0x18($t6)", "sw $zero, 0x1c($t6)",
                "li $t1, 3", "sw $t1, 0x4004($t0)", "lw $t2, 0x40($t7)", "sw $t2, 0x1000($zero)",
                "delay: lw $t3, 0x40($t7)", "subu $t4, $t3, $t2", "sltiu $t4, $t4, 400",
                "bnez $t4, delay", "nop", "sw $t3, 0x1004($zero)",
                "lw $v0, 0x4008($t0)", "sw $v0, 0x1008($zero)",
                "li $t1, 0x4ad", "sw $t1, 0x1004($t0)", "li $t1, 3", "sw $t1, 0x1000($t0)",
                "lw $v0, 0x100c($t0)", "sw $v0, 0x100c($zero)",
                "li $t1, 1", "sw $t1, 0x1200($t0)", "li $t1, 0x5bf", "sw $t1, 0x1004($t0)",
                "li $t1, 3", "sw $t1, 0x1000($t0)", "lw $v0, 0x1008($t0)",
                "sw $v0, 0x1010($zero)", "break"]
        code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
        instructions = list(struct.unpack("<%dI" % (len(code)//4), bytes(code)))
    (root / "code.bin").write_bytes(struct.pack("<%dI" % len(instructions), *instructions))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin")}] }))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    machine = object.__new__(MT6878Machine)
    from types import SimpleNamespace
    machine.loader = SimpleNamespace(capability_report={"rom_sha256": "a"*64})
    profile = None if software is None else dict(schema="firmwire.software-rf/v1", name="synthetic",
        analysis_only=True, assumptions="native test values only", rom_sha256="a"*64,
        ports={"0": {"chip_id": software[0], "eco": software[1]}})
    if software:
        profile["ports"]["0"]["reset_registers"] = {"341": {
            "value": 0x34567 if software[0] == 8 else 0xabcde,
            "source": "analysis-assumption", "reason": "synthetic native storage test"}}
    if rcal:
        profile["ports"]["0"]["calibration"] = dict(kind="mt6177m-rcal-analysis/v1",
            source="analysis-assumption", reason="synthetic native RCAL test", cw10=rcal[0], cw11=rcal[1], trim5=rcal[2])
    if ldo is not None:
        profile["ports"]["0"]["ldo_calibration"] = dict(kind="mt6177m-ldo-analysis/v1",
            source="analysis-assumption", reason="synthetic native LDO test", trims={"8":ldo[0], "64":ldo[1]})
    if rx_rc is not None:
        profile["ports"]["0"]["rx_rc_calibration"] = dict(kind="mt6177m-rx-rc-analysis/v1",
            source="analysis-assumption", reason="synthetic native RX RC test", trim6=rx_rc)
    if rx_tpd is not None:
        a,b,seed_a,seed_b = rx_tpd
        port = profile["ports"]["0"]
        port["rx_tpd_calibration"] = dict(kind="mt6177m-rx-tpd-analysis/v1",source="analysis-assumption",
            reason="synthetic native TPD test",cw423_trim4=a,cw429_trim4=b)
        port["reset_registers"].update({str(address):dict(value=value,source="analysis-assumption",reason="test backup seed")
            for address,value in ((469,seed_a),(472,seed_b))})
    devices = {base: BSIImmediatePeripheral("bank-%x" % base, base, 0x9000,
               bsi_mode="software-rf" if software else "capture-writes" if capture else "pending",
               rf_profile=profile, firmwire_machine=machine) for base in (0x400000, 0x600000)}
    blocks = 0
    from firmwire.vendor.mtk.hw.GCRPeripheral import GCRCustom_Periph
    from firmwire.vendor.mtk.hw.guest_clock import bind_rf_clock
    gcr = GCRCustom_Periph("gcr",0x800000,0x1000,firmwire_machine=machine)
    if software:
        bind_rf_clock({"GCRCustom":gcr, **devices})
    @panda.cb_after_block_exec
    def clock(cpu, tb, exit_code):
        nonlocal blocks
        blocks += 1

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base + 0x9000:
                result = device.hw_write(address-base, size, value)
                if read_completion and address == base + 0x1000 and value == 3:
                    # Explicit test backend supplies unrelated 36-bit data. No
                    # production code, firmware identity or poll chooses it.
                    pending = device.control.pending[0]
                    device.control.complete_read(0, pending.sequence, 0xa12345678)
                return result
        return False

    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, value):
        if 0x800000 <= address < 0x801000:
            value[0] = gcr.hw_read(address-0x800000,size)
            return True
        for base, device in devices.items():
            if base <= address < base + 0x9000:
                value[0] = device.hw_read(address-base, size)
                return True
        return False

    @panda.cb_before_handle_exception
    def exception(cpu, index):
        output = root / "result.json"
        if not output.exists():
            report = {"exception_index": index, "pc": int(panda.libpanda.panda_current_pc(cpu)),
                      "guest_words": list(struct.unpack("<5I" if read_completion or software else "<4I",
                          panda.physical_memory_read(0x1000, 20 if read_completion or software else 16))),
                      "devices": [device.control_observation() for device in devices.values()]}
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(output)
        return index
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeBsiTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_tpd_preserves_backup_bits_and_gates_results(self):
        for mode,values in (("--child-rx-tpd",(5,11,0xa5d23,0x35e45)),
                            ("--child-rx-tpd-other",(0,15,0,0xfffff))):
            report = self.run_child(mode)
            a,b,x,y=values
            self.assertEqual(report["exception_index"],18)
            self.assertEqual(report["guest_words"],[a<<11,b<<11,x&0xffc00,y&0xffc00,0])
            device,other = report["devices"]
            facts=device["serial_targets"]["0"]["rx_tpd_calibration"]
            self.assertEqual((facts["completions"],facts["reads"],facts["ready"]),(1,{"423":1,"429":1},False))
            self.assertEqual(other["serial_targets"]["0"]["rx_tpd_calibration"]["completions"],0)
            self.assertEqual(len(device["pending"]),2)

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_rx_rc_result_writeback_and_pending_reads(self):
        for mode,trim in (("--child-rx-rc",23),("--child-rx-rc-zero",0),("--child-rx-rc-max",63)):
            report = self.run_child(mode)
            self.assertEqual(report["exception_index"],18)
            self.assertEqual(report["guest_words"],[0,trim<<14,0x1bf00000 | trim<<14 | trim<<8,0,0])
            a,b = report["devices"]
            facts = a["serial_targets"]["0"]["rx_rc_calibration"]
            self.assertEqual((facts["completions"],facts["reads"],facts["ready"]),(1,1,False))
            self.assertEqual(b["serial_targets"]["0"]["rx_rc_calibration"]["completions"],0)
            self.assertEqual(len(a["pending"]),2)  # early request is not silently retried

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_timer_wait_orders_por_write_before_immediate_read(self):
        for mode,payload in (("--child-clock",0x21485),("--child-clock-other",0xabcde)):
            report = self.run_child(mode)
            self.assertEqual(report["exception_index"],18)
            start,end,status,value,pending = report["guest_words"]
            self.assertGreaterEqual(end-start,400)
            self.assertEqual((status,value,pending),(0x30,payload,0))
            a,b = report["devices"]
            self.assertEqual(a["hwpor"]["completed_writes"],1)
            self.assertEqual(b["hwpor"]["completed_writes"],0)
            self.assertEqual(a["pending"][0]["data"][0],0x5bf)
            self.assertFalse(a["clock_policy"]["bsi_polling_advances_time"])
            self.assertTrue(a["clock_policy"]["gcr_reads_advance_time"])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_ldo_clear_trim_latch_and_unanswered_next_stage(self):
        for mode, trims in (("--child-ldo", (7,19)), ("--child-ldo-other", (0,31))):
            report = self.run_child(mode)
            self.assertEqual(report["exception_index"], 18)
            self.assertEqual(report["guest_words"], [0,trims[0]<<15,0,trims[1]<<15,0])
            a,b = report["devices"]
            facts = a["serial_targets"]["0"]["ldo_calibration"]
            self.assertEqual((facts["completions"],facts["clear_reads"],facts["result_reads"]),(2,2,2))
            self.assertEqual(b["serial_targets"]["0"]["ldo_calibration"]["completions"],0)
            self.assertEqual(a["pending"][0]["data"][0],0x5bf)

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_rcal_sequence_returns_configured_results_not_blanket_success(self):
        for mode, expected in (("--child-rcal", [0x84210, 0x739ce, 16 << 10, 0, 0]),
                               ("--child-rcal-other", [1, 0xfffff, 31 << 10, 0, 0])):
            report = self.run_child(mode)
            self.assertEqual(report["exception_index"], 18)
            self.assertEqual(report["guest_words"], expected)
            a, b = report["devices"]
            self.assertEqual(a["serial_targets"]["0"]["calibration"]["completions"], 1)
            self.assertEqual(a["serial_targets"]["0"]["calibration"]["reads"], 3)
            self.assertEqual(b["serial_targets"]["0"]["calibration"]["completions"], 0)
            self.assertEqual(len(a["pending"]), 2)  # earlier read is NOT retried by polling/setup

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_software_identity_hwpor_and_readback_with_distinct_profiles(self):
        for mode, identity in (("--child-software", 8), ("--child-software-coful", 0x2c)):
            report = self.run_child(mode)
            self.assertEqual(report["exception_index"], 18)
            self.assertEqual(report["guest_words"], [identity, 0x30, 0x12345, 0,
                                                   0x34567 if identity == 8 else 0xabcde])
            a, b = report["devices"]
            self.assertEqual(a["hwpor"]["completed_writes"], 1)
            self.assertEqual(a["serial_targets"]["0"]["reads"], 3)
            self.assertEqual(b["hwpor"]["completed_writes"], 0)
            self.assertFalse(a["serial_targets"]["0"]["silicon_verified"])
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1",
                         "requires explicit development PANDA opt-in")
    def test_guest_sees_ready_then_pending_and_other_banks_are_isolated(self):
        report = self.run_child()
        self.assertEqual((report["exception_index"], report["pc"]), (18, 56))
        self.assertEqual(report["guest_words"], [1, 0, 1, 1])
        command = report["devices"][0]["pending"][0]
        self.assertEqual((command["port"], command["data"][0]), (3, 0x1234))
        self.assertEqual(report["devices"][1]["pending"], [])
        self.assertFalse(report["devices"][0]["backend_connected"])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1",
                         "requires explicit development PANDA opt-in")
    def test_guest_reads_explicit_backend_payload_and_acknowledges(self):
        report = self.run_child("--child-read")
        self.assertEqual((report["exception_index"], report["pc"]), (18, 64))
        self.assertEqual(report["guest_words"], [1, 0x12345678, 0xa, 0, 0])
        self.assertEqual(report["devices"][0]["completed_reads"], 1)
        self.assertEqual(report["devices"][1]["completed_reads"], 0)

    def run_child(self, mode="--child"):
        with tempfile.TemporaryDirectory(prefix="mtk-bsi-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            path = Path(directory) / "result.json"
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), mode, directory],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
            finally:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
            log.seek(0)
            self.assertTrue(path.exists(), log.read()[-3000:])
            return json.loads(path.read_text())

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI") == "1", "requires development PANDA")
    def test_guest_capture_write_releases_bank_but_read_remains_unanswered(self):
        report = self.run_child("--child-capture")
        self.assertEqual((report["exception_index"], report["pc"]), (18, 72))
        self.assertEqual(report["guest_words"], [1, 0, 0, 1])
        device = report["devices"][0]
        self.assertEqual(device["completed_writes"], 1)
        self.assertEqual(device["completed_reads"], 0)
        self.assertEqual(device["pending"][0]["data"][0], 0x400)
        self.assertTrue(device["backend_connected"])
        self.assertFalse(device["rf_emulated"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--child", "--child-read", "--child-capture", "--child-software", "--child-software-coful", "--child-rcal", "--child-rcal-other", "--child-ldo", "--child-ldo-other", "--child-clock", "--child-clock-other", "--child-rx-rc", "--child-rx-rc-zero", "--child-rx-rc-max", "--child-rx-tpd", "--child-rx-tpd-other"):
        software = {"--child-software": (8, 0), "--child-software-coful": (12, 2)}.get(sys.argv[1])
        rcal = {"--child-rcal": (0x84210, 0x739ce, 16), "--child-rcal-other": (1, 0xfffff, 31)}.get(sys.argv[1])
        ldo = {"--child-ldo": (7,19), "--child-ldo-other": (0,31)}.get(sys.argv[1])
        clock_trial = {"--child-clock":(0,0x21485),"--child-clock-other":(1023,0xabcde)}.get(sys.argv[1])
        rx_rc = {"--child-rx-rc":23,"--child-rx-rc-zero":0,"--child-rx-rc-max":63}.get(sys.argv[1])
        rx_tpd = {"--child-rx-tpd":(5,11,0xa5d23,0x35e45),"--child-rx-tpd-other":(0,15,0,0xfffff)}.get(sys.argv[1])
        child(sys.argv[2], sys.argv[1] == "--child-read", sys.argv[1] == "--child-capture", (8, 0) if rcal or ldo or clock_trial or rx_rc is not None or rx_tpd else software, rcal, ldo, clock_trial, rx_rc, rx_tpd)
    else: unittest.main()
