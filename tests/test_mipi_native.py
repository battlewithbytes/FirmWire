"""Native guest capture/isolation; not proof of real MIPI hardware semantics."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory, scheduler=False):
    from pandare import Panda
    from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
    from firmwire.vendor.mtk.machine import MT6878Machine
    if scheduler:
        from firmwire.vendor.mtk.hw.bsi_scheduler import MTKBsiSchedulerCapturePeripheral as Device
    else:
        from firmwire.vendor.mtk.hw.mipi import MTKMipiInitCapturePeripheral as Device
    root = Path(directory)
    devices = {base: Device("mipi", base, 0x5000, firmwire_machine=object.__new__(MT6878Machine))
               for base in (0x400000, 0x600000)}
    expected, instructions = [], []
    for i, (base, device) in enumerate(devices.items()):
        words = []
        for bank in ((0,) if scheduler else device.bank_offsets):
            for offset in device.WORD_OFFSETS:
                value = (0x12345678 ^ (i * 0xffffffff) ^ bank ^ offset) & 0xffffffff
                words.append(dict(offset=bank+offset, value=value))
                for written in ((0xffffffff, 0, value) if scheduler else (value,)):
                    instructions += [f"li $t0, {base}", f"li $t1, {written}", f"sw $t1, {bank+offset}($t0)"]
        expected.append(words)
    instructions += ["li $t1, 1", "sw $t1, 0x1100($zero)", "done: j done", "nop"]
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(instructions))
    (root / "code.bin").write_bytes(bytes(code))
    (root / "machine.json").write_text(json.dumps({"entry_address": 0, "memory_mapping": [
        {"name": "ram", "address": 0, "size": 0x200000, "file": str(root / "code.bin") }]}))
    panda = Panda(arch="mipsel", extra_args=["-M", "configurable", "-cpu", "cockpit-mtk-legacy",
        "-kernel", str(root / "machine.json"), "-display", "none", "-serial", "none", "-monitor", "none"])

    @panda.cb_unassigned_io_write
    def write(cpu, pc, address, size, value):
        for base, device in devices.items():
            if base <= address < base+0x5000:
                return device.hw_write(address-base, size, value)
        return False

    @panda.cb_after_block_exec
    def observe(cpu, tb, exit_code):
        if (root / "result.json").exists(): return
        if int.from_bytes(panda.physical_memory_read(0x1100, 4), "little") == 1:
            report = dict(facts=[d.control_observation() for d in devices.values()], expected=expected)
            (root / "result.tmp").write_text(json.dumps(report))
            (root / "result.tmp").replace(root / "result.json")

    if not hasattr(panda, "setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class MipiNativeTests(unittest.TestCase):
    def run_child(self, mode="--child"):
        with tempfile.TemporaryDirectory(prefix="mipi-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory) / "result.json"
            proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), mode, directory],
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
            return json.loads(result.read_text())

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_MIPI") == "1", "requires development PANDA")
    def test_guest_writes_all_banks_on_two_relocated_devices(self):
        report = self.run_child()
        for facts, expected in zip(report["facts"], report["expected"]):
            self.assertEqual(facts["registers"], expected)
            self.assertEqual((facts["reads"], facts["writes"]), (0, 40))
            self.assertIsNone(facts["capture_stop"])
            self.assertIsNone(facts["last_unsupported"])
            for key in ("semantics_verified", "boot_verified", "readback_supported",
                        "serial_transactions_supported", "completion_fabricated", "guest_irq_routed"):
                self.assertFalse(facts[key])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_BSI_SCHEDULER") == "1", "requires development PANDA")
    def test_guest_scheduler_writes_captured_without_claiming_enable_effects(self):
        report = self.run_child("--scheduler-child")
        self.assertEqual(len(report["facts"]), 2)
        for facts, expected in zip(report["facts"], report["expected"]):
            masks = [word["value"] for word in expected]
            self.assertEqual(facts["captured_words"]["registers"], expected)
            self.assertEqual(facts["captured_words"]["writes"], 24)
            self.assertIsNone(facts["enable_update_policy"])
            self.assertEqual([w["value"] for w in facts["recent_writes"]],
                             [word for mask in masks for word in (0xffffffff, 0, mask)])
            self.assertIsNone(facts["last_unsupported"])
            for key in ("semantics_verified", "boot_verified", "readback_supported",
                        "event_dispatch_supported", "completion_fabricated", "guest_irq_routed"):
                self.assertFalse(facts[key])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--child", "--scheduler-child"):
        child(sys.argv[2], scheduler=sys.argv[1] == "--scheduler-child")
    else: unittest.main()
