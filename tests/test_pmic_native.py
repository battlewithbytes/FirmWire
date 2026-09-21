"""Real peripheral composition and firmware-free PANDA MMIO regression."""
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


def devices(port=9, base=0x400000):
    from types import SimpleNamespace
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
    from firmwire.vendor.mtk.hw.PMICPeripheral import PMIC_WRAP_Periph
    from firmwire.vendor.mtk.hw.pmic import PmicRegisterMapAnalysis, PmicRegisterSpec
    machine = object.__new__(MT6878Machine)
    machine.loader = SimpleNamespace(capability_report={"rom_sha256": "a"*64})
    target = PmicRegisterMapAnalysis({0x24:PmicRegisterSpec(0x1357),
                                    0x42:PmicRegisterSpec(0xa5c0, 15)}, reason="synthetic native test")
    bsi = BSIImmediatePeripheral("bsi", base, 0x9000, firmwire_machine=machine,
                                bsi_mode="pending", pmic_target=target, pmic_port=port)
    wrapper = PMIC_WRAP_Periph("wacs", base+0x200000, 0x2000,
                              firmwire_machine=machine, pmic_target=target)
    other = PMIC_WRAP_Periph("other-wacs", base+0x400000, 0x2000,
        firmwire_machine=machine, pmic_target=PmicRegisterMapAnalysis(
            {0x24:PmicRegisterSpec(0x5678)}, reason="independent device test"))
    return machine, target, bsi, wrapper, other


@unittest.skipUnless(importlib.util.find_spec("avatar2"), "requires FirmWire dependencies")
class PmicPeripheralTests(unittest.TestCase):
    def test_constructor_shares_target_without_enabling_legacy_fallback(self):
        _, target, bsi, wrapper, other = devices()
        self.assertIs(bsi.control.serial_bus.targets[9].target, target)
        self.assertTrue(all(w.target is target for w in wrapper.wacs))
        self.assertIsNot(other.pmic_target, target)
        bsi.hw_write(0x1004, 4, 0x241111)
        bsi.hw_write(0x1000, 4, 0x901)
        wrapper.hw_write(0xc00, 4, 0x120000)
        self.assertEqual(wrapper.hw_read(0xc04, 4) & 0xffff, 0x1111)
        for device in (bsi, wrapper):
            with self.assertRaisesRegex(RuntimeError, "serialization"):
                device.pre_snapshot_handler("test")
        snapshot = wrapper.control_observation()
        snapshot["target"]["registers"]["36"]["value"] = 0
        self.assertEqual(target.read(0x24).value, 0x1111)

    def test_constructor_requires_explicit_unoccupied_port_and_target(self):
        from firmwire.vendor.mtk.hw.BSIPeripheral import BSIImmediatePeripheral
        machine, target, _, _, _ = devices()
        profile = dict(schema="firmwire.software-rf/v1", name="test", analysis_only=True,
                       assumptions="test", rom_sha256="a"*64, ports={"0":dict(chip_id=8,eco=0)})
        for kwargs in (dict(pmic_target=target), dict(pmic_port=4),
                       dict(pmic_target=object(), pmic_port=4, bsi_mode="pending"),
                       dict(pmic_target=target, pmic_port=True, bsi_mode="pending"),
                       dict(pmic_target=target, pmic_port=16, bsi_mode="pending"),
                       dict(pmic_target=target, pmic_port=4),
                       dict(pmic_target=target, pmic_port=0, bsi_mode="software-rf", rf_profile=profile)):
            with self.assertRaises(ValueError):
                BSIImmediatePeripheral("invalid", 0x400000, 0x9000, firmwire_machine=machine, **kwargs)
        composed = BSIImmediatePeripheral("composed", 0x400000, 0x9000, firmwire_machine=machine,
            bsi_mode="software-rf", rf_profile=profile, pmic_target=target, pmic_port=9)
        self.assertEqual(set(composed.control.serial_bus.targets), {0, 9})
        self.assertIs(composed.control.serial_bus.targets[9].target, target)

    def test_legacy_wrapper_behavior_is_not_silently_changed(self):
        from firmwire.vendor.mtk.hw.PMICPeripheral import PMIC_WRAP_Periph
        machine, _, _, _, _ = devices()
        legacy = PMIC_WRAP_Periph("legacy", 0x600000, 0x2000, firmwire_machine=machine)
        del legacy.pmic_target  # a legacy snapshot predating the new optional field
        legacy.hw_write(0xc00, 4, 4 << 16)
        self.assertEqual(legacy.hw_read(0xc04, 4) & 0xffff, 1)
        legacy.hw_write(0xc08, 4, 1)
        legacy.hw_write(0xc00, 4, 0x3cb << 16)
        self.assertEqual(legacy.hw_read(0xc04, 4) & 0xffff, 0)
        self.assertTrue(legacy.control_observation()["unknown_reads_return_zero"])
        legacy.pre_snapshot_handler("legacy")

    def test_strict_wrapper_rejects_partial_and_gap_accesses(self):
        _, _, _, wrapper, _ = devices()
        for offset, size in ((0xc04,1), (0xc04,2), (0xc05,4), (0xc0c,4), (0xc3c,4)):
            with self.assertRaises(ValueError): wrapper.hw_read(offset, size)
            with self.assertRaises(ValueError): wrapper.hw_write(offset, size, 0)


def child(directory, relocated):
    from pandare import Panda
    from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
    base, port = (0x1000000, 14) if relocated else (0x400000, 9)
    _, target, bsi, wrapper, other = devices(port, base)
    value = 0x2468 if relocated else 0xabcd
    asm = [f"li $t0, {base}", f"li $t1, {0x24 << 16 | value}", "sw $t1, 0x1004($t0)",
           f"li $t1, {port << 8 | 1}", "sw $t1, 0x1000($t0)",
           "lw $v0, 0x1008($t0)", "sw $v0, 0x1000($zero)",
           f"li $t2, {wrapper.address}", "li $t1, 0x120000", "sw $t1, 0xc00($t2)",
           "lw $v0, 0xc04($t2)", "sw $v0, 0x1004($zero)",
           "li $t1, 1", "sw $t1, 0xc08($t2)",
           "lw $v0, 0xc04($t2)", "sw $v0, 0x1008($zero)",
           f"li $t1, {0x80120000 | (value ^ 0xffff)}", "sw $t1, 0xc00($t2)",
           "lw $v0, 0xc04($t2)", "sw $v0, 0x100c($zero)",
           "li $t1, 0x120000", "sw $t1, 0xc10($t2)",
           "lw $v0, 0xc14($t2)", "sw $v0, 0x1010($zero)",
           f"li $t3, {other.address}", "sw $t1, 0xc00($t3)",
           "lw $v0, 0xc04($t3)", "sw $v0, 0x1014($zero)",
           "li $t1, 0x430000", "sw $t1, 0xc00($t2)",
           "lw $v0, 0xc04($t2)", "sw $v0, 0x1018($zero)",
           f"li $t1, {port << 8 | 3}", "sw $t1, 0x1100($t0)",
           "lw $v0, 0x1108($t0)", "sw $v0, 0x101c($zero)",
           "li $t1, 0x860000", "sw $t1, 0x1004($t0)",
           f"li $t1, {port << 8 | 1}", "sw $t1, 0x1000($t0)",
           "lw $v0, 0x1008($t0)", "sw $v0, 0x1020($zero)", "break"]
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
    root = Path(directory)
    (root / "code.bin").write_bytes(bytes(code))
    (root / "machine.json").write_text(json.dumps({"entry_address":0, "memory_mapping":[
        {"name":"ram", "address":0, "size":0x200000, "file":str(root / "code.bin")}] }))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    endpoints = (bsi, wrapper, other)

    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, result):
        for device in endpoints:
            if device.address <= address < device.address + device.size:
                result[0] = device.hw_read(address-device.address, size)
                return True
        return False

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for device in endpoints:
            if device.address <= address < device.address + device.size:
                return device.hw_write(address-device.address, size, value)
        return False

    @panda.cb_before_handle_exception
    def exception(cpu, index):
        if not (root / "result.json").exists():
            result = dict(exception_index=index,
                words=list(struct.unpack("<9I", panda.physical_memory_read(0x1000,36))),
                bsi=bsi.control_observation(), wrapper=wrapper.control_observation(),
                boot_verified=False)
            (root / "result.tmp").write_text(json.dumps(result))
            (root / "result.tmp").replace(root / "result.json")
        return index
    # Same pinned-PANDA signal-handler compatibility as the other native gates.
    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


@unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_PMIC") == "1", "requires development PANDA")
class PmicNativeTests(unittest.TestCase):
    def test_guest_cross_transport_state_and_unknowns_at_two_bases_and_ports(self):
        for mode, value in (("--child", 0xabcd), ("--child-relocated", 0x2468)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="mtk-pmic-") as directory, \
                    tempfile.TemporaryFile(mode="w+") as log:
                path = Path(directory) / "result.json"
                process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), mode, directory],
                                           stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic()+20
                    while not path.exists() and process.poll() is None and time.monotonic()<deadline:
                        time.sleep(.02)
                finally:
                    process.terminate()
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
                log.seek(0)
                self.assertTrue(path.exists(), log.read()[-4000:])
                report = json.loads(path.read_text())
                valid, idle, pending = 1 << 21 | 6 << 16, 1 << 21, 1 << 21 | 2 << 16
                self.assertEqual(report["exception_index"], 18)
                self.assertEqual(report["words"], [1, valid|value, idle, idle,
                    valid|(value ^ 0xffff), valid|0x5678, pending, 0, 0])
                self.assertEqual(len(report["bsi"]["pending"]), 2)
                self.assertEqual(report["bsi"]["completed_writes"], 1)
                self.assertEqual(report["wrapper"]["target"]["writes"], 2)
                self.assertFalse(report["boot_verified"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--child", "--child-relocated"):
        child(sys.argv[2], sys.argv[1] == "--child-relocated")
    else:
        unittest.main()
