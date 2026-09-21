"""Native halfword trigger/selector/result test at two unrelated MMIO bases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory):
    from pandare import Panda
    from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
    from firmwire.vendor.mtk.machine import MT6878Machine
    from firmwire.vendor.mtk.hw.AbbMixPeripheral import AbbMixAnalysisPeripheral
    from test_abbmix import model
    root = Path(directory)
    instructions = []
    for device_index, base in enumerate((0x400000, 0x600000)):
        instructions += [f"li $t0, {base}", "li $t1, 9", "sh $t1, 0($t0)",
                         "li $t1, 1", "sh $t1, 8($t0)", "sh $zero, 8($t0)"]
        for index in range(4):
            instructions += [f"li $t1, {index<<8}", "sh $t1, 10($t0)"]
            for bank, offset in enumerate((12,14)):
                destination=0x1000+device_index*32+index*8+bank*4
                instructions += [f"lhu $v0, {offset}($t0)", f"sw $v0, {destination}($zero)"]
    instructions += ["li $t1, 1", "sw $t1, 0x1100($zero)", "done: j done", "nop"]
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(instructions))
    (root / "code.bin").write_bytes(bytes(code))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin") }]}))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])
    devices = {base: AbbMixAnalysisPeripheral("abb", base, 0x1000, calibration=model(),
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000, 0x600000)}

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base+0x1000:
                return device.hw_write(address-base, size, value)
        return False

    @panda.cb_unassigned_io_read
    def read(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base+0x1000:
                value[0] = device.hw_read(address-base, size)
                return True
        return False

    @panda.cb_after_block_exec
    def observe(cpu, tb, exit_code):
        if (root / "result.json").exists(): return
        if int.from_bytes(panda.physical_memory_read(0x1100, 4), "little") == 1:
            report = dict(facts=[d.control_observation() for d in devices.values()],
                          words=[int.from_bytes(panda.physical_memory_read(x, 4), "little")
                                 for x in range(0x1000, 0x1040, 4)])
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(root / "result.json")

    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class AbbMixNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_ABBMIX") == "1", "requires development PANDA")
    def test_halfword_guest_selects_both_banks_at_two_bases(self):
        with tempfile.TemporaryDirectory(prefix="abbmix-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory) / "result.json"
            proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child", directory],
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+10
                while not result.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
            finally:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            log.seek(0)
            self.assertTrue(result.exists(), log.read()[-3000:])
            report = json.loads(result.read_text())
            expected=[(0x8000|v) for pair in zip([11,22,33,44],[100,200,300,400]) for v in pair]
            self.assertEqual(report["words"],expected*2)
            for facts in report["facts"]:
                self.assertEqual(facts["completions"],1)
                self.assertEqual(facts["result_reads"],[[1]*4,[1]*4])
                self.assertIsNone(facts["last_unsupported"])
                self.assertTrue(facts["analysis_only"])
                self.assertFalse(facts["analog_modelled"])



if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child": child(sys.argv[2])
    else: unittest.main()
