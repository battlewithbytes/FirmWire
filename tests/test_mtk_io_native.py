"""Native metadata observer must preserve both handled I/O and a real bus fault."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest


def child(directory,write_fault,observer_first):
    from pandare import Panda
    from test_mtk_io_observation import io,exception
    root=Path(directory)
    words=[0x3c080040,0x8d020020,0xac021000,0xad020024,0x3c080060,
           0xad020028 if write_fault else 0x8d020028,0x0000000d]
    (root/"code.bin").write_bytes(struct.pack("<%dI"%len(words),*words))
    (root/"machine.json").write_text(json.dumps({"entry_address":0,"memory_mapping":[
        {"name":"code","address":0,"size":0x200000,"file":str(root/"code.bin") }]}))
    panda=Panda(arch="mipsel",extra_args=["-M","configurable","-cpu","cockpit-mtk-legacy",
        "-kernel",str(root/"machine.json"),"-display","none","-serial","none","-monitor","none"])
    report={"execution":{"completed_blocks":0,"per_context":{}}}
    labels={}; handled=[]
    def observer(): return io.install_unassigned_io_observer(panda,report,labels)
    if observer_first: trace=observer()
    @panda.cb_unassigned_io_read
    def handler_read(cpu,pc,address,size,value):
        if address==0x400020:
            value[0]=0x13579bdf
            handled.append("read")
            return True
        return False
    @panda.cb_unassigned_io_write
    def handler_write(cpu,pc,address,size,value):
        if address==0x400024:
            handled.append(["write",int(value)])
            return True
        return False
    if not observer_first: trace=observer()
    def persist():
        if (root/"result.json").exists(): return
        report["guest_word"]=int.from_bytes(panda.physical_memory_read(0x1000,4),"little")
        report["handled"]=handled
        (root/"result.tmp").write_text(json.dumps(report))
        (root/"result.tmp").replace(root/"result.json")
    exception.install_exception_observer(panda,report,labels,persist,io_trace=trace)
    if not hasattr(panda,"setup_internal_signal_handler"):
        panda.setup_internal_signal_handler=panda._setup_internal_signal_handler
    panda.athread.warned=True
    panda.run()


class NativeIoTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FIRMWIRE_TEST_NATIVE_IO")=="1","requires development PANDA")
    def test_reads_writes_and_callback_order_do_not_suppress_bus_faults(self):
        for write in (False,True):
            for first in (False,True):
                with tempfile.TemporaryDirectory(prefix="mtk-io-") as directory, tempfile.TemporaryFile(mode="w+") as log:
                    path=Path(directory)/"result.json"
                    proc=subprocess.Popen([sys.executable,"-B",str(Path(__file__).resolve()),
                        "--child",directory,str(int(write)),str(int(first))],stdout=log,stderr=subprocess.STDOUT)
                    try:
                        deadline=time.monotonic()+10
                        while not path.exists() and proc.poll() is None and time.monotonic()<deadline: time.sleep(.02)
                    finally:
                        proc.terminate()
                        try: proc.wait(timeout=2)
                        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
                    log.seek(0); self.assertTrue(path.exists(),log.read()[-3000:])
                    report=json.loads(path.read_text())
                    self.assertEqual(report["guest_word"],0x13579bdf)
                    self.assertEqual(report["handled"],["read",["write",0x13579bdf]])
                    event=report["execution"]["cpu_exceptions"]["first"][0]
                    self.assertEqual(event["exception_index"],28)
                    access=event["preceding_unassigned_io"][-1]
                    self.assertEqual((access["physical_address"],access["size"],access["direction"]),
                                     (0x600028,4,"write" if write else "read"))
                    self.assertNotIn("value",access)
                    self.assertFalse(report["execution"]["unassigned_io"]["handles_accesses"])


if __name__=="__main__":
    if len(sys.argv)==5 and sys.argv[1]=="--child": child(sys.argv[2],bool(int(sys.argv[3])),bool(int(sys.argv[4])))
    else: unittest.main()
