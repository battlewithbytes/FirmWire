"""A UART-generated level causes real guest ISR entry, acknowledgement and ERET.

This is synthetic direct wiring, NOT a substitute for a modem's MDCIRQ ABI.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory, model):
    from pandare import Panda
    from keystone import Ks, KS_ARCH_MIPS, KS_MODE_MIPS32, KS_MODE_LITTLE_ENDIAN
    from firmwire.hw.uart import UARTCore, UARTRegisterBank
    from firmwire.emulator.mips_irq import MipsIRQInput
    root = Path(directory)
    # The inherited MTK map is not standard kseg translation. Keep synthetic
    # physical mappings explicit for each CPU model, just as a board must.
    ram_base = 0x80000000 if model == "cockpit-mtk-legacy" else 0
    uart_base = 0xa0400000 if model == "cockpit-mtk-legacy" else 0x400000
    assembler = Ks(KS_ARCH_MIPS, KS_MODE_MIPS32 | KS_MODE_LITTLE_ENDIAN)
    main, _ = assembler.asm("""
        mtc0 $zero, $12
        li $t0, 0xa0400000
        li $t7, 0x80000000
        li $t1, 1
        sw $t1, 4($t0)
        sw $t1, 0x1100($t7)
        j enabled
        nop
    enabled:
        mfc0 $t2, $13
        sw $t2, 0x1000($t7)
        li $t1, 0x2001
        mtc0 $t1, $12
        nop
    wait_isr:
        lw $t2, 0x1008($t7)
        beqz $t2, wait_isr
        nop
        mfc0 $t2, $13
        sw $t2, 0x100c($t7)
        li $t1, 1
        sw $t1, 0x1104($t7)
    done:
        j done
        nop
    """,addr=0x80000000)
    handler, _ = assembler.asm("""
        mfc0 $k0, $13
        sw $k0, 0x1004($t7)
        lui $k1, 0xa040
        lw $k0, 0($k1)
        sw $k0, 0x1008($t7)
        eret
        nop
    """,addr=0x80000180)
    code = bytearray(0x2000)
    code[:len(main)], code[0x180:0x180+len(handler)] = bytes(main), bytes(handler)
    (root/"code.bin").write_bytes(code)
    (root/"machine.json").write_text(json.dumps({"entry_address":0x80000000,"memory_mapping":[
        {"name":"ram","address":ram_base,"size":0x200000,"file":str(root/"code.bin") }]}))
    panda = Panda(arch="mipsel",extra_args=["-M","configurable","-cpu",model,
        "-kernel",str(root/"machine.json"),"-display","none","-serial","none","-monitor","none"])
    endpoint = MipsIRQInput(panda,0,5)
    levels, exceptions = [], []
    def irq(level):
        levels.append(level)
        endpoint(level)
    core = UARTCore(16, irq)
    bank = UARTRegisterBank(core,stride=4,access_sizes=(4,))
    @panda.cb_unassigned_io_write
    def write(cpu,pc,address,size,value):
        if uart_base <= address < uart_base+0x20:
            bank.write(address-uart_base,size,value)
            return True
        return False
    @panda.cb_unassigned_io_read
    def read(cpu,pc,address,size,value):
        if uart_base <= address < uart_base+0x20:
            value[0] = bank.read(address-uart_base,size)
            return True
        return False
    @panda.cb_before_handle_exception
    def exception(cpu,index):
        exceptions.append(int(index))
        if len(exceptions) <= 8:
            print("exception",index,hex(int(panda.libpanda.panda_current_pc(cpu))),flush=True)
        return index
    injected = False
    blocks = 0
    recent = []
    @panda.cb_before_block_exec
    def before(cpu,tb):
        if blocks < 8:
            print("before",hex(int(panda.libpanda.panda_current_pc(cpu))),flush=True)
    @panda.cb_after_block_exec
    def observe(cpu,tb,exit_code):
        nonlocal injected, blocks
        blocks += 1
        recent.append(int(panda.libpanda.panda_current_pc(cpu)))
        del recent[:-16]
        def word(address): return int.from_bytes(panda.physical_memory_read(ram_base+address,4),"little")
        if word(0x1100) and not injected:
            injected = True
            core.receive(b"Z")
        if blocks <= 8 or blocks == 1024:
            (root/"debug.json").write_text(json.dumps(dict(recent=recent, exceptions=exceptions[:16],
                levels=levels, words=[word(0x1000+i*4) for i in range(4)], injected=injected)))
        if word(0x1104) and not (root/"result.json").exists():
            rejects = [endpoint._set_irq(*args) for args in ((-1,5,1),(999,5,1),(0,1,1),(0,8,1),(0,5,2))]
            report = dict(words=[word(0x1000+i*4) for i in range(4)],levels=levels,
                          exceptions=exceptions,rejects=rejects,rx_remaining=len(core.rx))
            (root/"result.tmp").write_text(json.dumps(report))
            (root/"result.tmp").replace(root/"result.json")
    if not hasattr(panda,"setup_internal_signal_handler"):
        panda.setup_internal_signal_handler = panda._setup_internal_signal_handler
    panda.athread.warned = True
    panda.run()


class MipsIRQNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_IRQ") == "1", "requires IRQ development engine")
    def test_masked_pending_isr_read_deasserts_and_eret_resumes(self):
        for model in ("24Kc", "cockpit-mtk-legacy"):
            with self.subTest(model=model):
                self.check_model(model)

    def check_model(self, model):
        with tempfile.TemporaryDirectory(prefix="irq-native-") as directory, tempfile.TemporaryFile(mode="w+") as log:
            result = Path(directory)/"result.json"
            proc = subprocess.Popen([sys.executable,"-B",str(Path(__file__).resolve()),"--child",directory,model],
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
            debug = Path(directory)/"debug.json"
            self.assertTrue(result.exists(),log.read()[-2000:] + (debug.read_text() if debug.exists() else ""))
            report = json.loads(result.read_text())
            before, handler, byte, after = report["words"]
            self.assertTrue(before & 0x2000)
            self.assertTrue(handler & 0x2000)
            self.assertEqual(handler & 0x7c,0)  # architectural interrupt ExcCode
            self.assertEqual(byte,ord("Z"))
            self.assertFalse(after & 0x2000)
            self.assertEqual(report["levels"],[True,False])
            self.assertEqual(report["rx_remaining"],0)
            self.assertEqual(report["rejects"],[-1,-2,-1,-1,-1])


if __name__ == "__main__":
    if len(sys.argv)==4 and sys.argv[1]=="--child": child(sys.argv[2],sys.argv[3])
    else: unittest.main()
