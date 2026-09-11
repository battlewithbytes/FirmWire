import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("exception_observation", Path(__file__).resolve().parents[1] /
    "firmwire/vendor/mtk/exception_observation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExceptionObservationTests(unittest.TestCase):
    def test_trace_is_bounded_and_preserves_exception_indices(self):
        trace = module.ExceptionTrace()
        for i in range(1000):
            self.assertEqual(trace.record(i, 0x90000000, "context-0", i, list(range(100))), i)
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["events"], 1000)
        self.assertEqual(len(snapshot["first"]), 32)
        self.assertEqual(len(snapshot["recent"]), 32)
        self.assertEqual(len(snapshot["counts"]), 65)
        self.assertEqual(len(snapshot["recent"][0]["preceding_pcs"]), 16)
        self.assertFalse(snapshot["registers_or_payloads_recorded"])

    def test_snapshot_does_not_alias_live_records(self):
        trace = module.ExceptionTrace()
        recent = [123]
        trace.record(18, 0x90000000, "context-0", 100, recent)
        recent[0] = 456
        snapshot = trace.snapshot()
        snapshot["first"][0]["preceding_pcs"][0] = 789
        self.assertEqual(trace.snapshot()["first"][0]["preceding_pcs"], [123])

    def test_real_callback_contract_is_observation_only(self):
        callbacks = []
        panda = SimpleNamespace(cb_before_handle_exception=lambda callback: callbacks.append(callback),
            libpanda=SimpleNamespace(panda_current_pc=Mock(return_value=0x90a65842)))
        report = {"execution": {"completed_blocks": 100, "per_context": {}}}
        labels = {}
        persist = Mock()
        module.install_exception_observer(panda, report, labels, persist)
        for index in (-1, 0, 8, 18, 20, 29):
            self.assertEqual(callbacks[0]("opaque-cpu", index), index)
        self.assertEqual(persist.call_count, 6)
        self.assertEqual(report["execution"]["cpu_exceptions"]["recent"][-1]["pc"], 0x90a65842)
        self.assertEqual(labels, {"opaque-cpu": "context-0"})

    def test_exception_flood_bounds_checkpoint_writes(self):
        callbacks = []
        panda = SimpleNamespace(cb_before_handle_exception=callbacks.append,
            libpanda=SimpleNamespace(panda_current_pc=lambda cpu: 0x90000000))
        report = {"execution": {"completed_blocks": 0, "per_context": {}}}
        persist = Mock()
        trace = module.install_exception_observer(panda, report, {}, persist)
        for _ in range(1000):
            self.assertEqual(callbacks[0]("cpu", 18), 18)
        self.assertEqual(persist.call_count, 36)  # first 32; 64, 128, 256, 512
        self.assertEqual(trace.snapshot()["events"], 1000)
        self.assertEqual(report["execution"]["cpu_exceptions"]["events"], 512)
