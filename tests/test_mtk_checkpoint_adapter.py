"""Exercise the actual adapter method with synthetic maps and callback objects.

Extract its AST to avoid loading native emulator libraries in these unit tests.
No firmware image, proprietary constants or device keys are required.
"""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "firmwire/vendor/mtk"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observation = load("observation")
exceptions = load("exception_observation")
tree = ast.parse((ROOT / "machine.py").read_text())
method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
              and node.name == "_install_execution_evidence")
namespace = {"json": json, "log": Mock(), "__package__": "firmwire.vendor.mtk",
    **{name: getattr(observation, name) for name in
       ("RamObservation", "validate_pc_markers", "record_pc_marker")}}
exec(compile(ast.Module(body=[method], type_ignores=[]), "machine.py", "exec"), namespace)
install = namespace["_install_execution_evidence"]


class CheckpointAdapterTests(unittest.TestCase):
    def make_machine(self, profile=None):
        self.saved = []
        self.callbacks = {}
        report = {"rom_sha256": "a" * 64}
        def register(name):
            def decorator(callback):
                self.callbacks[name] = callback
                return callback
            return decorator
        self.reader = Mock(return_value=b"\x01\0\0\0")
        self.machine = SimpleNamespace(
            loader=SimpleNamespace(capability_report=report,
                loader_args={"observe_ram": profile},
                write_capability_report=lambda: self.saved.append(copy.deepcopy(report))),
            peripheral_map={},
            panda=SimpleNamespace(cb_after_block_exec=register("block"),
                cb_before_handle_exception=register("exception"),
                physical_memory_read=self.reader,
                libpanda=SimpleNamespace(panda_current_pc=lambda _: 0x2000)),
            avatar=SimpleNamespace(memory_ranges=SimpleNamespace(at=lambda address: [
                SimpleNamespace(begin=0x1000, end=0x2000, data=SimpleNamespace(
                    forwarded=False, is_special=False, is_symbolic=False, permissions="rw"))])))
        return self.machine

    def profile(self, directory, **kwargs):
        config = dict(schema="cockpit.mtk-ram-observer/v1", rom_sha256="a" * 64,
                      words=[0x1000], pc_markers={"entry": 0x2000})
        config.update(kwargs)
        path = Path(directory) / "observer.json"
        path.write_text(json.dumps(config))
        return str(path)

    def test_default_observer_does_not_read_ram_or_register_exception_hook(self):
        install(self.make_machine())
        self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
        self.reader.assert_not_called()
        self.assertNotIn("exception", self.callbacks)

    def test_first_marker_persists_once_between_periodic_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            install(self.make_machine(self.profile(directory)))
            callback = self.callbacks["block"]
            callback("cpu", SimpleNamespace(pc=0x3000), 0)
            self.reader.return_value = b"\x02\0\0\0"
            callback("cpu", SimpleNamespace(pc=0x2000), 0)  # block 2, not periodic
            execution = self.saved[-1]["execution"]
            self.assertEqual(execution["observed_ram_at_block"], 2)
            marker = execution["per_context"]["context-0"]["pc_markers"]["entry"]
            self.assertEqual(marker["first_ram"], {"0x1000": 2})
            writes = len(self.saved)
            callback("cpu", SimpleNamespace(pc=0x2000), 0)
            self.assertEqual(len(self.saved), writes)

    def test_exception_checkpoint_refreshes_ram_without_changing_exception(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules,
                {"firmwire.vendor.mtk.exception_observation": exceptions}):
            install(self.make_machine(self.profile(directory, cpu_exceptions=True)))
            self.callbacks["block"]("cpu", SimpleNamespace(pc=0x3000), 0)
            self.reader.return_value = b"\x03\0\0\0"
            self.assertEqual(self.callbacks["exception"]("cpu", 18), 18)
            self.assertEqual(self.saved[-1]["execution"]["observed_ram"], {"0x1000": 3})
            self.assertEqual(self.saved[-1]["execution"]["cpu_exceptions"]["events"], 1)

    def test_other_image_profile_is_refused_before_callbacks_or_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory, rom_sha256="b" * 64))
            with self.assertRaises(ValueError):
                install(machine)
            self.assertEqual(self.callbacks, {})
            self.reader.assert_not_called()

    def test_non_boolean_exception_option_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                install(self.make_machine(self.profile(directory, cpu_exceptions="true")))
            self.assertEqual(self.callbacks, {})
            self.reader.assert_not_called()
