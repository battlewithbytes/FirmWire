"""Digital sequencer tests; synthetic backends never certify an RF target."""
import importlib.util
import json
import os
from pathlib import Path
import pickle
import sys
import unittest

spec = importlib.util.spec_from_file_location("hwpor_unit", Path(__file__).resolve().parents[1] /
    "firmwire/vendor/mtk/hw/hwpor.py")
hwpor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hwpor
spec.loader.exec_module(hwpor)


class HwporTests(unittest.TestCase):
    def model(self, submit=None, registers=0x100, data=0x800):
        return hwpor.HwporSequencer(hwpor.HwporLayout(registers, data), submit)

    def event(self, model, event, first, words, time=10, port=5):
        l = model.layout
        model.write(l.registers, model.read(l.registers) | 1 << event)
        model.write(l.registers + 16 + 8*event, time)
        model.write(l.registers + 20 + 8*event, ((first + len(words)-1) << 16) | first)
        for i, word in enumerate(words):
            model.write(l.data + 8*(first+i), word)
            model.write(l.data + 8*(first+i) + 4, port)

    def trigger(self, model):
        model.write(model.layout.registers + 4, 1)
        model.write(model.layout.registers + 12, 0xffff)
        model.write(model.layout.registers + 4, 5)  # clear strobe, not a trigger
        self.assertEqual(model.triggers, 0)
        model.write(model.layout.registers + 4, 3)

    def test_relocated_registers_slots_ports_and_time_order(self):
        for base, data in ((0x100, 0x800), (0x4000, 0x8000)):
            calls = []
            def submit(write):
                calls.append(write)
                return True
            m = self.model(submit, base, data)
            self.event(m, 3, 125, [0x123, 0x456, 0x789], time=20, port=15)
            self.event(m, 9, 0, [0xabcdef], time=10, port=2)
            self.trigger(m)
            m.advance(9)
            self.assertEqual(calls, [])
            m.advance(1)
            self.assertEqual(m.status, 3 << 18)
            m.advance(10)
            self.assertEqual([(c.event, c.slot, c.port) for c in calls],
                             [(9, 0, 2), (3, 125, 15), (3, 126, 15), (3, 127, 15)])
            self.assertEqual(m.status, (3 << 18) | (3 << 6))

    def test_polling_and_time_do_not_complete_or_resubmit_pending(self):
        calls = []
        def submit(write):
            calls.append(write)
            return False
        m = self.model(submit)
        self.event(m, 2, 4, [10, 20])
        self.trigger(m)
        m.advance(10)
        for _ in range(100):
            self.assertEqual(m.read(m.layout.registers + 8), 0)
            m.advance(1)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError): m.complete(999)
        m.complete(calls[0].sequence)
        self.assertEqual(m.status, 0)  # last slot has not completed
        m.advance(0)
        self.assertEqual(len(calls), 2)
        m.complete(calls[1].sequence)
        self.assertEqual(m.status, 3 << 4)

    def test_no_backend_cannot_complete(self):
        m = self.model()
        self.event(m, 0, 0, [123])
        self.trigger(m)
        m.advance(100)
        self.assertIsNotNone(m.pending)
        self.assertEqual(m.status, 0)

    def test_enable_and_clear_do_not_trigger_and_disabled_force_is_inert(self):
        m = self.model(lambda w: True)
        self.event(m, 0, 0, [123])
        for value in (0, 1, 5, 2):
            m.write(m.layout.registers + 4, value)
        m.advance(100)
        self.assertEqual(m.triggers, 0)

    def test_status_is_readonly_and_clear_uses_one_bit_per_event(self):
        m = self.model(lambda w: True)
        self.event(m, 1, 0, [1])
        self.event(m, 7, 8, [2])
        self.trigger(m)
        m.advance(10)
        m.write(m.layout.registers + 8, 0xffffffff)
        self.assertEqual(m.status, (3 << 2) | (3 << 14))
        m.write(m.layout.registers + 12, 1 << 7)
        self.assertEqual(m.status, 3 << 2)

    def test_snapshot_is_not_mutated_by_later_slot_writes(self):
        calls = []
        def submit(write):
            calls.append(write.word)
            return True
        m = self.model(submit)
        self.event(m, 0, 0, [123])
        self.trigger(m)
        m.write(m.layout.data, 456)
        m.advance(10)
        self.assertEqual(calls, [123])

    def test_invalid_event_preflight_has_no_partial_backend_writes(self):
        for config in (0x80000000, 2 | (1 << 16)):
            m = self.model(lambda w: self.fail("must not dispatch"))
            self.event(m, 0, 0, [123])
            self.event(m, 1, 1, [456])
            m.write(m.layout.registers + 28, config)
            with self.assertRaises((ValueError, NotImplementedError)):
                m.write(m.layout.registers + 4, 3)
            self.assertEqual(m.queue, [])
            self.assertEqual(m.triggers, 0)

    def test_missing_slot_and_mipi_configuration_are_refused(self):
        for missing in (True, False):
            m = self.model()
            self.event(m, 0, 0, [123])
            if missing:
                del m.storage[m.layout.data]
            else:
                m.write(m.layout.data + 4, 0x100)
            with self.assertRaises((ValueError, NotImplementedError)):
                self.trigger(m)

    def test_busy_retrigger_refused_reset_invalidates_old_completions(self):
        m = self.model()
        self.event(m, 0, 0, [1])
        self.trigger(m)
        m.advance(10)
        old = m.pending.sequence
        for value in (3, 5):
            with self.assertRaises(ValueError): m.write(m.layout.registers + 4, value)
        m.reset()
        self.event(m, 0, 0, [2])
        self.trigger(m)
        m.advance(10)
        with self.assertRaises(ValueError): m.complete(old)
        self.assertNotEqual(m.pending.sequence, old)

    def test_bad_backend_reply_is_not_a_completion(self):
        for reply in (None, 1, "done"):
            m = self.model(lambda w: reply)
            self.event(m, 0, 0, [1])
            self.trigger(m)
            with self.assertRaises(ValueError): m.advance(10)
            self.assertEqual(m.status, 0)
            self.assertIsNotNone(m.pending)

    def test_layout_access_time_and_unsupported_trigger_validation(self):
        for args in ((0, 4), (-4, 0x800), (1, 0x800), (0, 0x800, 17), (0, 0x800, 16, 129)):
            with self.assertRaises(ValueError): hwpor.HwporLayout(*args)
        m = self.model()
        for offset, size in ((0, 4), (0x101, 4), (0x100, 2)):
            with self.assertRaises(ValueError): m.read(offset, size)
        for ticks in (-1, True, 1.5):
            with self.assertRaises(ValueError): m.advance(ticks)
        with self.assertRaises(NotImplementedError): m.write(0x104, 8)
        with self.assertRaises(ValueError): m.write(0x104, 7)

    def test_snapshot_roundtrip_and_instances_are_independent(self):
        a, b = self.model(), self.model()
        self.event(a, 0, 0, [42])
        self.trigger(a)
        a.advance(10)
        restored = pickle.loads(pickle.dumps(a))
        restored.complete(a.pending.sequence)
        self.assertEqual(restored.status, 3)
        self.assertEqual(a.status, 0)
        self.assertEqual(b.facts()["queued_writes"], 0)
        self.assertFalse(restored.facts()["firmware_boot_verified"])

    @unittest.skipUnless(os.environ.get("FIRMWIRE_RF_POR_INVENTORIES"), "needs Cockpit RF inventories")
    def test_real_extracted_tables_program_sequencer_without_runtime_shortcut(self):
        paths = json.loads(os.environ["FIRMWIRE_RF_POR_INVENTORIES"])
        self.assertEqual(len(paths), 2)
        variants_seen = 0
        for path in paths:
            doc = json.loads(Path(path).read_text())
            for variant in doc["variants"]:
                variants_seen += 1
                calls = []
                def submit(write):
                    calls.append((write.event, write.slot, write.port, write.word))
                    return True
                m = self.model(submit, 0x4000, 0x8000)
                expected = []
                descriptors = variant["data_table"]["records"]
                # This is a test driver for the guest register writers, NOT
                # a production path to preload tables or pick an ECO variant.
                for event, desc, array in zip(variant["event_table"]["records"], descriptors,
                                              variant["command_arrays"]):
                    index, start = event["event_index"], event["first_slot"]
                    words = [w["raw"] for w in array["words"]]
                    port = desc["port_argument"]
                    time = (event["time_argument"] * 75) & 0xfffff
                    self.event(m, index, start, words, time=time, port=port)
                    expected.extend((time, index, start+i, port, word) for i, word in enumerate(words))
                self.trigger(m)
                m.advance(0xfffff)
                self.assertEqual(calls, [e[1:] for e in sorted(expected)])
                self.assertEqual(m.completed_writes, len(expected))
                self.assertEqual(m.status, sum(3 << (2*e["event_index"])
                                              for e in variant["event_table"]["records"]))
        self.assertEqual(variants_seen, 3)
