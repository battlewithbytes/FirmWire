import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


def load(name):
    spec = importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/
        ("firmwire/vendor/mtk/"+name+".py"))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


io=load("io_observation")
exception=load("exception_observation")


class IoObservationTests(unittest.TestCase):
    def test_bounded_context_association_and_defensive_copy(self):
        trace=io.UnassignedIoTrace()
        for i in range(1000): trace.record(2,0x1234,4,i%2,"a" if i%2 else "b",i)
        snap=trace.snapshot()
        self.assertEqual(snap["events"],1000)
        self.assertEqual(snap["counts"],{"read":500,"write":500})
        self.assertEqual((len(snap["first"]),len(snap["recent"])),(32,32))
        self.assertFalse(snap["payloads_recorded"])
        self.assertFalse(snap["handles_accesses"])
        events=trace.preceding("a",998)
        self.assertEqual(len(events),8)
        self.assertTrue(all(e["context"]=="a" and e["completed_blocks"]<=998 for e in events))
        events[-1]["physical_address"]=0
        snap["recent"][-1]["physical_address"]=0
        self.assertEqual(trace.snapshot()["recent"][-1]["physical_address"],0x1234)
        self.assertEqual(trace.preceding("unknown",1000),[])

    def test_callbacks_never_touch_read_buffer_or_accept_access(self):
        reads,writes,exceptions=[],[],[]
        panda=SimpleNamespace(cb_unassigned_io_read=reads.append,cb_unassigned_io_write=writes.append,
            cb_before_handle_exception=exceptions.append,libpanda=SimpleNamespace(panda_current_pc=lambda cpu:2))
        report={"execution":{"completed_blocks":7,"per_context":{}}}
        labels={}; persist=Mock()
        trace=io.install_unassigned_io_observer(panda,report,labels)
        exception.install_exception_observer(panda,report,labels,persist,io_trace=trace)
        unreadable=object()  # dereferencing or modifying it would fail
        self.assertIs(reads[0]("cpu",2,0x160b0024,4,unreadable),False)
        self.assertIs(writes[0]("cpu",2,0x160b0024,4,0xdeadbeef),False)
        self.assertIs(writes[0]("other",4,0x600000,2,123),False)
        self.assertEqual(exceptions[0]("cpu",28),28)
        events=report["execution"]["cpu_exceptions"]["first"][0]["preceding_unassigned_io"]
        self.assertEqual(len(events),2)
        self.assertEqual([e["direction"] for e in events],["read","write"])
        self.assertTrue(all(e["physical_address"]==0x160b0024 for e in events))
        self.assertNotIn("value",events[-1])
        self.assertEqual(report["execution"]["unassigned_io"]["events"],3)
        self.assertFalse(report["execution"]["cpu_exceptions"]["registers_or_payloads_recorded"])
        persist.assert_called_once()


if __name__=="__main__": unittest.main()
