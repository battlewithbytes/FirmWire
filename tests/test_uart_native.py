"""Actual guest MMIO through two relocated UART adapters; no modem fixture."""
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
    from firmwire.vendor.mtk.hw.idc_uart import MTKIDCUARTPeripheral
    root = Path(directory)
    asm = ["li $t0, 0x400000"]
    for offset, value in ((0x24,3),(0xc,0x80),(0,1),(4,0),(0x28,5),(0x2c,2),
                          (0x54,0x75),(0x84,0x75),(0x58,0),(0x88,0),
                          (0xc,3),(0x50,1),(8,0xc7),(4,1)):
        asm += [f"li $t1, {value}", f"sw $t1, {offset}($t0)"]
    for index, offset in enumerate((0xc,0x24,0x54,0x5c,0x14,8)):
        asm += [f"lw $v0, {offset}($t0)", f"sw $v0, {0x1000+index*4}($zero)"]
    # Last pattern slot as well as the first: cover layout, not a single boot write.
    for offset, value in ((0xc4, 0), (0xc8, 0x3d), (0xcc, 0xf1), (0xc0, 0x72), (0xc4, 0x55), (0xfc, 0xa6)):
        asm += [f"li $t1, {value}", f"sw $t1, {offset}($t0)"]
    for index, offset in enumerate((0xc0,0xc4,0xc8,0xcc,0xfc)):
        asm += [f"lbu $v0, {offset}($t0)", f"sw $v0, {0x101c+index*4}($zero)"]
    asm += ["li $t1, 0x5a", "sw $t1, 0($t0)",
            "li $t0, 0x600000", "lw $v0, 0x54($t0)", "sw $v0, 0x1018($zero)",
            "lw $v0, 0xc4($t0)", "sw $v0, 0x1030($zero)",
            "li $t1, 1", "sw $t1, 0x1100($zero)", "j .", "nop"]
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("\n".join(asm))
    (root/"code.bin").write_bytes(bytes(code))
    (root/"machine.json").write_text(json.dumps({"entry_address":0,"memory_mapping":[
        {"name":"ram","address":0,"size":0x200000,"file":str(root/"code.bin") }]}))
    panda = Panda(arch="mipsel", extra_args=["-M","configurable","-cpu","cockpit-mtk-legacy",
        "-kernel",str(root/"machine.json"),"-display","none","-serial","none","-monitor","none"])
    devices = {base: MTKIDCUARTPeripheral("uart",base,0x1000,
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000,0x600000)}
    @panda.cb_unassigned_io_write
    def write(cpu,pc,address,size,value):
        for base, device in devices.items():
            if base <= address < base+0x1000:
                return device.hw_write(address-base,size,value)
        return False
    @panda.cb_unassigned_io_read
    def read(cpu,pc,address,size,value):
        for base, device in devices.items():
            if base <= address < base+0x1000:
                value[0] = device.hw_read(address-base,size)
                return True
        return False
    @panda.cb_after_block_exec
    def done(cpu,tb,exit_code):
        if (root/"result.json").exists(): return
        if int.from_bytes(panda.physical_memory_read(0x1100,4),"little") == 1:
            words = [int.from_bytes(panda.physical_memory_read(0x1000+i*4,4),"little") for i in range(13)]
            result = {"words": words, "tx": list(devices[0x400000].registers.core.drain_tx()),
                      "rx": list(devices[0x400000].registers.core.rx)}
            (root/"result.tmp").write_text(json.dumps(result))
            (root/"result.tmp").replace(root/"result.json")
    if not hasattr(panda,"setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class NativeUARTTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_UART") == "1", "requires development PANDA")
    def test_guest_configuration_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix="uart-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory)/"result.json"
            proc = subprocess.Popen([sys.executable,"-B",str(Path(__file__).resolve()),"--child",directory],
                                    stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+15
                while not result.exists() and proc.poll() is None and time.monotonic()<deadline:
                    time.sleep(.02)
            finally:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
            log.seek(0)
            self.assertTrue(result.exists(),log.read()[-3000:])
            report = json.loads(result.read_text())
            self.assertEqual(report["words"], [3,3,0x75,0xc1,0x60,0xc1,0,0x72,0x55,0x3d,0xf1,0xa6,0])
            self.assertEqual(report["tx"],[0x5a])
            self.assertEqual(report["rx"],[])


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--child": child(sys.argv[2])
    else: unittest.main()
