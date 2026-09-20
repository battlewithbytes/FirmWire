"""Guest MMIO verifies passive RR storage at two unrelated physical bases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory, control=False, analysis=False):
    from pandare import Panda
    from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.lte_timer import MTKLTETimerRRPeripheral, MTKLTETimerControlPeripheral
    from firmwire.vendor.mtk.hw.lte_timer import MTKLTETimerInitStorageAnalysisPeripheral
    root = Path(directory)
    program = """
        li $t0, 0x400000
        li $t1, 0x12345678
        sw $t1, 0x5c($t0)
        li $t1, 0x87654321
        sw $t1, 0x7c($t0)
        lw $v0, 0x5c($t0)
        sw $v0, 0x1000($zero)
        lw $v0, 0x7c($t0)
        sw $v0, 0x1004($zero)
        li $t0, 0x600000
        li $t1, 0xabcdef01
        sw $t1, 0x5c($t0)
        lw $v0, 0x5c($t0)
        sw $v0, 0x1008($zero)
    """
    if control:
        program += """
        li $t0, 0x400000
        li $t1, 0x12345678
        sw $t1, 0x4a4($t0)
        lw $v0, 0x4a4($t0)
        sw $v0, 0x100c($zero)
        sw $zero, 0x4a4($t0)
        lw $v0, 0x4a4($t0)
        sw $v0, 0x1010($zero)
        sw $t1, 0x4a4($t0)
        lw $v0, 0x4a4($t0)
        sw $v0, 0x1014($zero)
        """
    if analysis:
        program += """
        li $t0, 0x400000
        sw $zero, 0x4ec($t0)
        li $t1, 0xffffffff
        sw $t1, 0x4f0($t0)
        lw $v0, 0x4ec($t0)
        sw $v0, 0x1018($zero)
        lw $v0, 0x4f0($t0)
        sw $v0, 0x101c($zero)
        li $t0, 0x600000
        li $t1, 0x13579bdf
        sw $t1, 0x4ec($t0)
        lw $v0, 0x4ec($t0)
        sw $v0, 0x1020($zero)
        """
    program += """
        li $t1, 1
        sw $t1, 0x1100($zero)
    done:
        j done
        nop
    """
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm(program)
    (root / "code.bin").write_bytes(bytes(code))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin") }]}))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    device_class = MTKLTETimerControlPeripheral if control else MTKLTETimerRRPeripheral
    if analysis:
        device_class = MTKLTETimerInitStorageAnalysisPeripheral
    devices = {base: device_class("timer", base, 0x2000,
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000, 0x600000)}

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base + 0x2000:
                return device.hw_write(address - base, size, value)
        return False

    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base + 0x2000:
                value[0] = device.hw_read(address - base, size)
                return True
        return False

    @panda.cb_after_block_exec
    def observe(cpu, tb, exit_code):
        if (root / "result.json").exists():
            return
        if int.from_bytes(panda.physical_memory_read(0x1100, 4), "little") == 1:
            report = dict(facts=[d.control_observation() for d in devices.values()],
                          words=[int.from_bytes(panda.physical_memory_read(x, 4), "little")
                                 for x in range(0x1000, 0x1024 if analysis else 0x1018 if control else 0x100c, 4)])
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(root / "result.json")

    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class LTETimerNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_LTE_TIMER") == "1", "requires development PANDA")
    def test_guest_configuration_roundtrip_and_instance_isolation(self):
        self.run_case(False)

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_LTE_TIMER") == "1", "requires development PANDA")
    def test_guest_source_mask_program_disable_restore(self):
        self.run_case(True)

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_LTE_TIMER") == "1", "requires development PANDA")
    def test_guest_provisional_word_storage_is_labeled_and_isolated(self):
        self.run_case(True, True)

    def run_case(self, control, analysis=False):
        with tempfile.TemporaryDirectory(prefix="lte-timer-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory) / "result.json"
            proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                     "--analysis-child" if analysis else "--control-child" if control else "--child", directory],
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not result.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            log.seek(0)
            self.assertTrue(result.exists(), log.read()[-3000:])
            report = json.loads(result.read_text())
            self.assertEqual(report["words"], [0x12345678, 0x87654321, 0xabcdef01] +
                             ([0x12345678, 0, 0x12345678] if control else []) +
                             ([0, 0xffffffff, 0x13579bdf] if analysis else []))
            expected_counts = [7, 2] if analysis else [5 if control else 2, 1]
            self.assertEqual([f["writes"] for f in report["facts"]], expected_counts)
            self.assertEqual([f["reads"] for f in report["facts"]], expected_counts)
            for facts in report["facts"]:
                self.assertFalse(facts["clock_supported"])
                self.assertFalse(facts["guest_irq_routed"])
                self.assertIsNone(facts["last_unsupported"])
                if analysis:
                    self.assertTrue(facts["analysis_only"])
                    self.assertFalse(facts["semantics_verified"])
                    self.assertFalse(facts["boot_verified"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--child", "--control-child", "--analysis-child"):
        child(sys.argv[2], sys.argv[1] != "--child", sys.argv[1] == "--analysis-child")
    else:
        unittest.main()
