"""Guest MMIO verifies the narrow IDC enable contract at arbitrary addresses."""
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
    from firmwire.vendor.mtk.hw.idc_control import MTKIDCControlPeripheral
    root = Path(directory)
    code, _ = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN).asm("""
        li $t0, 0x400000
        li $t1, 1
        sw $t1, 0xc($t0)
        lw $v0, 0x10($t0)
        sw $v0, 0x1000($zero)
        lw $v0, 0x10($t0)
        sw $v0, 0x1004($zero)
        li $t0, 0x600000
        lw $v0, 0x10($t0)
        sw $v0, 0x1008($zero)
        sw $t1, 0x1100($zero)
    done:
        j done
        nop
    """)
    (root/"code.bin").write_bytes(bytes(code))
    (root/"machine.json").write_text(json.dumps({"entry_address":0,"memory_mapping":[
        {"name":"ram","address":0,"size":0x200000,"file":str(root/"code.bin") }]}))
    panda = Panda(arch="mipsel",extra_args=["-M","configurable","-cpu","cockpit-mtk-legacy",
        "-kernel",str(root/"machine.json"),"-display","none","-serial","none","-monitor","none"])
    devices = {base: MTKIDCControlPeripheral("idc",base,0x1000,
               firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000,0x600000)}
    @panda.cb_unassigned_io_write
    def write(cpu,pc,address,size,value):
        for base, device in devices.items():
            if base <= address < base+0x1000: return device.hw_write(address-base,size,value)
        return False
    @panda.cb_unassigned_io_read
    def read(cpu,pc,address,size,value):
        for base, device in devices.items():
            if base <= address < base+0x1000:
                value[0] = device.hw_read(address-base,size)
                return True
        return False
    @panda.cb_after_block_exec
    def observe(cpu,tb,exit_code):
        if (root/"result.json").exists(): return
        if int.from_bytes(panda.physical_memory_read(0x1100,4),"little") == 1:
            report = dict(facts=[d.control_observation() for d in devices.values()],
                          words=list(panda.physical_memory_read(0x1000,12)))
            (root/"result.tmp").write_text(json.dumps(report))
            (root/"result.tmp").replace(root/"result.json")
    if not hasattr(panda,"setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class IDCCounterNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_IDC") == "1", "requires development PANDA")
    def test_guest_counter_enable_and_instance_isolation(self):
        with tempfile.TemporaryDirectory(prefix="idc-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory)/"result.json"
            proc = subprocess.Popen([sys.executable,"-B",str(Path(__file__).resolve()),"--child",directory],
                                    stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+10
                while not result.exists() and proc.poll() is None and time.monotonic()<deadline: time.sleep(.02)
            finally:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
            log.seek(0)
            self.assertTrue(result.exists(),log.read()[-3000:])
            report = json.loads(result.read_text())
            self.assertEqual(report["words"],[0]*12)
            self.assertEqual([f["enable_writes"] for f in report["facts"]],[1,0])
            self.assertEqual([f["count_reads"] for f in report["facts"]],[2,1])
            self.assertTrue(report["facts"][0]["enabled"])
            self.assertFalse(report["facts"][1]["enabled"])


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--child": child(sys.argv[2])
    else: unittest.main()
