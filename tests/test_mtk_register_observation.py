import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("register_observation",
    Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk/register_observation.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class RegisterObservationTests(unittest.TestCase):
    def test_validation(self):
        for config in ({}, {"a": 1}, {"a": True}, {"a": 2**32}, {"a": 2, "b": 2},
                       {str(i): i * 2 for i in range(33)}):
            with self.assertRaises(ValueError):
                observer.validate_v0_trace(config)

    def test_filtered_bounded_read_only_trace(self):
        class Panda:
            libpanda = SimpleNamespace(panda_get_retval_external=lambda cpu: 0x12345678)
            def cb_insn_translate(self, fn): self.translate = fn; return fn
            def cb_insn_exec(self, fn): self.execute = fn; return fn
        panda = Panda()
        report = {"execution": {"completed_blocks": 100}}
        saved = []
        trace = observer.install_v0_trace(panda, {"point": 4}, report, {}, lambda: saved.append(1))
        self.assertFalse(panda.translate(None, 8))
        self.assertTrue(panda.translate(None, 4))
        panda.execute("cpu", 8)
        self.assertEqual(trace["total_events"], 0)
        for i in range(300):
            self.assertEqual(panda.execute("cpu", 4), 0)
        self.assertEqual(trace["total_events"], 300)
        self.assertEqual(len(trace["events"]), 256)
        self.assertEqual(len(saved), 2)
        self.assertEqual(trace["events"][-1]["v0"], 0x12345678)


if __name__ == "__main__":
    unittest.main()
