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
    def test_stack_capture_is_selected_bounded_and_non_mutating(self):
        sp, validate, read = Mock(return_value=0x1000), Mock(), Mock(return_value=b"\x07\0\0\0" * 2)
        capture = module.ExceptionStackCapture({"pcs": [0x2000], "exception_index": 18, "words": 2},
                                               sp, validate, read)
        self.assertIsNone(capture.sample("cpu", 20, 0x2000))
        self.assertIsNone(capture.sample("cpu", 18, 0x2002))
        sp.assert_not_called()
        for _ in range(4):
            self.assertEqual(capture.sample("cpu", 18, 0x2000)["words"], [7, 7])
        self.assertIsNone(capture.sample("cpu", 18, 0x2000))
        self.assertEqual(read.call_count, 4)
        validate.assert_called_with([0x1000, 0x1004])
        read.assert_called_with(0x1000, 8)

    def test_stack_validation_precedes_reads_and_errors_are_evidence(self):
        read = Mock()
        capture = module.ExceptionStackCapture({"pcs": [2], "exception_index": 18, "words": 1},
            lambda _: 0x1000, Mock(side_effect=ValueError("MMIO forbidden")), read)
        self.assertIn("MMIO forbidden", capture.sample("cpu", 18, 2)["error"])
        read.assert_not_called()
        capture = module.ExceptionStackCapture({"pcs": [2], "exception_index": 18, "words": 1},
            lambda _: 0x1000, Mock(), Mock(return_value=b""))
        self.assertIn("Short stack read", capture.sample("cpu", 18, 2)["error"])

    def test_stack_config_and_snapshot_safety(self):
        good = {"pcs": [2], "exception_index": 18, "words": 1}
        for bad in ({}, dict(good, words=True), dict(good, words=17), dict(good, pcs=[3]),
                    dict(good, pcs=[2, 2]), dict(good, exception_index=True), dict(good, extra=1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.ExceptionStackCapture(bad, Mock(), Mock(), Mock())
        trace = module.ExceptionTrace()
        stack = {"words": [7]}
        trace.record(18, 2, "cpu", 1, [], stack)
        stack["words"][0] = 8
        self.assertEqual(trace.snapshot()["first"][0]["stack"]["words"], [7])
        self.assertTrue(trace.snapshot()["registers_or_payloads_recorded"])

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

    def test_capture_disclosure_survives_eviction_from_recent_events(self):
        trace = module.ExceptionTrace()
        for i in range(100):
            trace.record(18, 2, "cpu", i, [], {"words": [7]} if i == 40 else None)
        snapshot = trace.snapshot()
        self.assertTrue(snapshot["registers_or_payloads_recorded"])
        self.assertTrue(all("stack" not in event for event in snapshot["first"] + snapshot["recent"]))

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
