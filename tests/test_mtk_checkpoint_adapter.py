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
    "bind_rf_clock": load("hw/guest_clock").bind_rf_clock,
    **{name: getattr(observation, name) for name in
       ("RamObservation", "validate_pc_markers", "record_pc_marker", "normal_tb_exit")}}
exec(compile(ast.Module(body=[method], type_ignores=[]), "machine.py", "exec"), namespace)
install = namespace["_install_execution_evidence"]


class CheckpointAdapterTests(unittest.TestCase):
    def test_scheduling_samples_only_executed_checkpoints_with_explicit_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory))
            machine.loader.capability_report["engine_topology"] = {"realized_cpu_objects": 4}
            with patch("firmwire.emulator.scheduling_observation.SchedulingObserver") as observer:
                observer.return_value.sample.return_value = {"read_only": True}
                install(machine)
                observer.assert_called_once_with(machine.panda, 4)
                for code in (2,3): self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), code)
                observer.return_value.sample.assert_not_called()
                self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
                observer.return_value.sample.assert_called_once_with(1)
                self.assertEqual(machine.loader.capability_report["execution"]["scheduling"], {"read_only":True})

    def test_topology_alone_does_not_enable_scheduling_diagnostics(self):
        machine = self.make_machine()
        machine.loader.capability_report["engine_topology"] = {"realized_cpu_objects": 4}
        with patch("firmwire.emulator.scheduling_observation.SchedulingObserver") as observer:
            install(machine)
            self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
            observer.assert_not_called()
            self.assertNotIn("scheduling", machine.loader.capability_report["execution"])

    def test_nonexecuted_tb_exits_do_not_count_or_sample_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory))
            install(machine)
            callback = self.callbacks["block"]
            before = self.reader.call_count
            for code in (2, 3, 3):
                callback("cpu", SimpleNamespace(pc=0x2000), code)
            execution = machine.loader.capability_report["execution"]
            self.assertEqual(execution["completed_blocks"], 0)
            self.assertEqual(execution["per_context"], {})
            self.assertEqual(execution["early_exit_callbacks"], {"2":1,"3":2})
            self.assertEqual(self.reader.call_count, before)
            for code in (0, 1): callback("cpu", SimpleNamespace(pc=0x2000), code)
            self.assertEqual(execution["completed_blocks"], 2)
            self.assertEqual(execution["per_context"]["context-0"]["pc_markers"]["entry"]["hits"],2)

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
                cb_unassigned_io_read=register("io_read"),
                cb_unassigned_io_write=register("io_write"),
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
        self.assertNotIn("io_read", self.callbacks)

    def test_byte_only_changes_are_persisted_without_changing_word_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory,
                byte_windows={"code": {"address": 0x1100, "size": 8}}))
            state = [0]
            self.reader.side_effect = lambda address, size: bytes([state[0] if address == 0x1100 else 1])*size
            install(machine)
            callback = self.callbacks["block"]
            callback("cpu", SimpleNamespace(pc=0x2000), 0)
            state[0] = 7
            machine.loader.capability_report["execution"]["completed_blocks"] = 9
            callback("cpu", SimpleNamespace(pc=0x2000), 0)
            execution = self.saved[-1]["execution"]
            self.assertEqual(execution["observed_ram_bytes"]["code"]["hex"], "07"*8)
            self.assertEqual(len(execution["ram_changes"]), 2)
            self.assertEqual(execution["ram_changes"][0]["words"], execution["ram_changes"][1]["words"])

    def test_idc_control_observer_is_explicit_available_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory,peripheral_controls=["IDC_CTRL"]))
            control = SimpleNamespace(enable_control_observer=Mock(),
                                      control_observation=lambda: {"enabled":True,"transmit_count":0})
            machine.peripheral_map["IDC_CTRL"] = control
            install(machine)
            self.callbacks["block"]("cpu",SimpleNamespace(pc=0x2000),0)
            control.enable_control_observer.assert_called_once()
            self.assertEqual(self.saved[-1]["execution"]["peripheral_controls"]["IDC_CTRL"],
                             {"enabled":True,"transmit_count":0})
        for names in (["IDC_CTRL"],["IDC_CTRL","IDC_CTRL"],["unknown"]):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                install(self.make_machine(self.profile(directory,peripheral_controls=names)))

    def test_io_observer_is_explicit_and_persisted_with_exception(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules,{
            "firmwire.vendor.mtk.exception_observation":exceptions,
            "firmwire.vendor.mtk.io_observation":load("io_observation")}):
            install(self.make_machine(self.profile(directory,cpu_exceptions=True,unassigned_io=True)))
            self.callbacks["block"]("cpu",SimpleNamespace(pc=0x2000),0)
            self.assertIs(self.callbacks["io_write"]("cpu",0x2000,0x160b0024,4,3),False)
            self.assertEqual(self.callbacks["exception"]("cpu",28),28)
            execution=self.saved[-1]["execution"]
            self.assertEqual(execution["unassigned_io"]["events"],1)
            self.assertEqual(execution["cpu_exceptions"]["first"][0]["preceding_unassigned_io"][-1]["physical_address"],0x160b0024)
            self.assertTrue(self.saved[-1]["ram_observer"]["unassigned_io"])

    def test_ring_observer_requires_real_device_and_accepts_relocated_name(self):
        from firmwire.vendor.mtk.hw.PCCIFPeripheral import SHM_CCIF_Periph
        for real in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                machine = self.make_machine(self.profile(directory, peripheral_controls=["OTHER_SHM"]))
                device = object.__new__(SHM_CCIF_Periph) if real else SimpleNamespace()
                device.enable_control_observer = Mock()
                device.control_observation = lambda: {"read_only": True}
                machine.peripheral_map["OTHER_SHM"] = device
                if not real:
                    with self.assertRaisesRegex(ValueError, "Unsupported control observer"):
                        install(machine)
                    continue
                install(machine)
                self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
                device.enable_control_observer.assert_called_once()
                self.assertEqual(self.saved[-1]["execution"]["peripheral_controls"]["OTHER_SHM"],
                                 {"read_only": True})

    def test_pmic_observer_requires_selected_wrapper_and_identical_target(self):
        for name in ("SYNTHETIC_PMIC_A", "RELOCATED_WRAPPER_B"):
            with tempfile.TemporaryDirectory() as directory:
                machine = self.make_machine(self.profile(directory, peripheral_controls=[name]))
                target = object()
                control = SimpleNamespace(pmic_target=target, enable_control_observer=Mock(),
                    control_observation=lambda: {"kind": "shared-pmic-wrapper-analysis/v1"})
                machine.loader.pmic_analysis_binding = SimpleNamespace(wrapper_name=name, target=target)
                machine.peripheral_map[name] = control
                install(machine)
                self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
                control.enable_control_observer.assert_called_once()
                self.assertEqual(self.saved[-1]["execution"]["peripheral_controls"][name],
                                 {"kind": "shared-pmic-wrapper-analysis/v1"})
        for mismatch in ("no-binding", "wrong-target", "wrong-name", "missing-wrapper"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(mismatch=mismatch):
                machine = self.make_machine(self.profile(directory, peripheral_controls=["PMIC_TEST"]))
                target = object()
                machine.loader.pmic_analysis_binding = SimpleNamespace(wrapper_name="PMIC_TEST", target=target)
                machine.peripheral_map["PMIC_TEST"] = SimpleNamespace(pmic_target=target,
                    enable_control_observer=Mock(), control_observation=Mock())
                if mismatch == "no-binding": machine.loader.pmic_analysis_binding = None
                if mismatch == "wrong-target": machine.peripheral_map["PMIC_TEST"].pmic_target = object()
                if mismatch == "wrong-name": machine.loader.pmic_analysis_binding.wrapper_name = "OTHER"
                if mismatch == "missing-wrapper": machine.peripheral_map.clear()
                with self.assertRaisesRegex(ValueError, "Unsupported control observer"): install(machine)

    def test_mdcirq_observer_requires_device_type_not_platform_name(self):
        from firmwire.vendor.mtk.hw.MDCPeripheral import MDCIRQ_Periph
        for real in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                machine = self.make_machine(self.profile(directory, peripheral_controls=["RELOCATED_IRQ"]))
                device = object.__new__(MDCIRQ_Periph) if real else SimpleNamespace()
                device.enable_control_observer = Mock()
                device.control_observation = lambda: {"read_only": True, "semantics_verified": False}
                machine.peripheral_map["RELOCATED_IRQ"] = device
                if not real:
                    with self.assertRaisesRegex(ValueError, "Unsupported control observer"):
                        install(machine)
                    continue
                install(machine)
                self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
                device.enable_control_observer.assert_called_once()
                self.assertFalse(self.saved[-1]["execution"]["peripheral_controls"]
                                 ["RELOCATED_IRQ"]["semantics_verified"])

    def test_pccif_observer_requires_device_type_not_platform_name(self):
        from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph
        for real in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                machine = self.make_machine(self.profile(directory, peripheral_controls=["RELOCATED_CCIF"]))
                device = object.__new__(PCCIF_Periph) if real else SimpleNamespace()
                device.enable_control_observer = Mock()
                device.control_observation = lambda: {"read_only": True, "semantics_verified": False}
                machine.peripheral_map["RELOCATED_CCIF"] = device
                if not real:
                    with self.assertRaisesRegex(ValueError, "Unsupported control observer"):
                        install(machine)
                    continue
                install(machine)
                self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
                device.enable_control_observer.assert_called_once()
                self.assertFalse(self.saved[-1]["execution"]["peripheral_controls"]
                                 ["RELOCATED_CCIF"]["semantics_verified"])

    def test_d2bif_observer_is_explicit_and_keeps_analysis_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = self.make_machine(self.profile(directory, peripheral_controls=["D2BIF", "LTE_TIMER"]))
            facts = {"analysis_only": True, "semantics_verified": False, "boot_verified": False}
            for name in ("D2BIF", "LTE_TIMER"):
                machine.peripheral_map[name] = SimpleNamespace(
                    enable_control_observer=Mock(), control_observation=lambda: dict(facts))
            install(machine)
            self.callbacks["block"]("cpu", SimpleNamespace(pc=0x2000), 0)
            for name in ("D2BIF", "LTE_TIMER"):
                machine.peripheral_map[name].enable_control_observer.assert_called_once()
                self.assertEqual(self.saved[-1]["execution"]["peripheral_controls"][name], facts)
        for names in (["D2BIF"], ["D2BIF", "D2BIF"], ["D2BIF", "unknown"]):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                install(self.make_machine(self.profile(directory, peripheral_controls=names)))

    def test_io_observer_rejects_truthy_non_boolean_and_requires_exceptions(self):
        for extra in ({"unassigned_io":"true"},{"unassigned_io":1},{"unassigned_io":True},
                      {"unassigned_io":True,"cpu_exceptions":False}):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                install(self.make_machine(self.profile(directory,**extra)))

    def test_rf_clock_is_global_and_independent_of_optional_observer(self):
        for observe in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                machine = self.make_machine(self.profile(directory) if observe else None)
                seq = SimpleNamespace(ticks=0,advance=Mock())
                gcr = SimpleNamespace(timer=0,bind_clock=Mock())
                device = SimpleNamespace(hwpor=seq,bind_clock=lambda c:c.attach(seq))
                machine.peripheral_map = {"GCRCustom":gcr,"bsi":device}
                install(machine)
                clock = gcr.bind_clock.call_args.args[0]
                callback = self.callbacks["block"]
                for i in range(1025): callback("cpu%d" % (i%2),SimpleNamespace(pc=0x3000),0)
                seq.advance.assert_not_called()  # No former 1024-block dispatch batch
                self.assertEqual(clock.synchronize(),0)
                seq.advance.assert_called_once_with(0)
                gcr.timer = 10
                self.assertEqual(clock.synchronize(),10)
                seq.advance.assert_called_with(750)
                self.assertEqual(machine.loader.capability_report["rf_guest_clock"],clock.facts())

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
