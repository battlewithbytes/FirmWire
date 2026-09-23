import copy
import unittest
from unittest.mock import Mock

from firmwire.hw.irq_inputs import IRQInputLatch
from firmwire.hw.routed_irq import RoutedLevelIRQController
from firmwire.vendor.mtk.hw.mdcirq_delivery import MdcirqLevelDelivery


class SoftwareIRQTests(unittest.TestCase):
    def test_packed_updates_have_no_transient_cpu_edges(self):
        edges = []
        core = RoutedLevelIRQController(8, [edges.append])
        for source in (1, 2):
            core.configure(source, priority=1, targets=[0])
            core.set_mask(source, False)
        latch = IRQInputLatch(8, core.set_levels)
        latch.update("software", [(1, True)])
        latch.update("software", [(1, False), (2, True)])
        self.assertEqual(edges, [True])
        before = core.snapshot()
        with self.assertRaises(ValueError): core.set_levels([(2, False), (9, True)])
        self.assertEqual(core.snapshot(), before)

    def delivery(self, source=6, count=2):
        self.lines = [[] for _ in range(count)]
        d = MdcirqLevelDelivery([line.append for line in self.lines], [3],
            minimum_inclusive=True, software_sources=[source])
        d.write(0x1a8, 4, 1)
        d.write(0x600, 4, (1 << count)-2)  # route group zero to CPU zero
        d.write(0x180 + 4*(source//32), 4, 1 << (source%32))
        return d

    def test_latch_owners_and_invalid_batch_are_independent(self):
        calls = []
        latch = IRQInputLatch(8, calls.append)
        latch.update("external", [(3, True)])
        latch.update("software", [(3, True)])
        latch.update("software", [(3, False)])
        self.assertEqual(calls[-1], [(3, True)])
        latch.update("external", [(3, False)])
        self.assertEqual(calls[-1], [(3, False)])
        before = copy.deepcopy(vars(latch))
        for updates in ([(3, True), (8, False)], [(3, True), (3, False)], [(True, True)], [(3, 1)]):
            with self.assertRaises(ValueError): latch.update("software", updates)
            self.assertEqual(vars(latch), before)

    def test_masked_post_duplicate_claim_clear_and_return(self):
        for source in (6, 76, 175, 255):
            d = self.delivery(source)
            word, bit = source//32, 1 << (source%32)
            for _ in range(2): d.write(0x140+4*word, 4, bit)
            self.assertTrue(d.inputs.software[source])
            self.assertEqual(self.lines, [[], []])
            d.write(0x40+4*word, 4, bit)
            self.assertEqual(self.lines, [[True], []])
            self.assertEqual(d.read(0xc20, 4), source)
            self.assertEqual(d.bank.core.active[0], [(source, 127)])
            d.write(0x120+4*word, 4, bit)
            self.assertFalse(d.inputs.software[source])
            self.assertEqual(d.bank.core.active[0], [(source, 127)])
            d.write(0xc70, 4, 0x1ff)
            self.assertEqual(d.bank.core.active, [[], []])
            self.assertEqual(self.lines, [[True, False], []])

    def test_post_while_active_survives_completion(self):
        d = self.delivery()
        d.write(0x40, 4, 64)
        d.write(0x140, 4, 64)
        d.read(0xc20, 4)
        d.write(0x120, 4, 64)
        d.write(0x140, 4, 64)
        d.write(0xc70, 4, 0x1ff)
        self.assertEqual(self.lines[0], [True, False, True])
        self.assertEqual(d.read(0xc20, 4), 6)

    def test_broadcast_single_destination_and_fail_closed_route_change(self):
        d = self.delivery(count=4)
        d.write(0xc0, 4, 64)
        d.write(0x600, 4, 11)  # only CPU 2
        d.write(0x40, 4, 64)
        d.write(0x140, 4, 64)
        self.assertEqual(self.lines, [[], [], [True], []])
        before = d.snapshot()
        with self.assertRaises(NotImplementedError): d.write(0x600, 4, 0)
        self.assertEqual(d.snapshot(), before)
        self.assertEqual(d.read(0xc28, 4), 6)
        d.write(0x120, 4, 64)
        d.write(0xc78, 4, 0x1ff)

    def test_requires_guest_level_mode_and_refuses_nmi(self):
        d = self.delivery()
        for register in (0x160, 0x100):
            d.write(register, 4, 64)
            before = d.snapshot()
            with self.assertRaises(NotImplementedError): d.write(0x140, 4, 64)
            self.assertEqual(d.snapshot(), before)
            d.write(0x180, 4, 64)
        d.write(0x100, 4, 0)
        d.write(0x140, 4, 64)
        for register in (0x160, 0x100):
            before = d.snapshot()
            with self.assertRaises(NotImplementedError): d.write(register, 4, 64)
            self.assertEqual(d.snapshot(), before)

    def test_direct_bank_and_readback_preserve_other_inputs(self):
        d = self.delivery()
        d.write(0x80, 4, 64 | 128)
        self.assertEqual(d.merge_software_read(0x80, 4, 128), 192)
        d.write(0x120, 4, 64)
        self.assertEqual(d.merge_software_read(0x80, 4, 192), 128)
        d.set_level(3, True)
        d.write(0x120, 4, 8)
        self.assertTrue(d.bank.core.levels[3])
        d.write(0x140, 4, 64)
        d.write(0x80, 4, 0)
        self.assertFalse(d.inputs.software[6])

    def test_adapter_shared_source_retains_external_owner_on_clear(self):
        d = MdcirqLevelDelivery([Mock()], [6], minimum_inclusive=True, software_sources=[6])
        d.write(0x1a8, 4, 1)
        d.write(0x180, 4, 64)
        d.set_level(6, True)
        d.write(0x140, 4, 64)
        d.write(0x120, 4, 64)
        self.assertTrue(d.bank.core.levels[6])
        self.assertFalse(d.inputs.software[6])
        d.set_level(6, False)
        self.assertFalse(d.bank.core.levels[6])

    def test_malformed_and_mixed_unsupported_write_is_atomic(self):
        d = self.delivery()
        for offset, size, value, error in ((0x140, 4, 72, NotImplementedError),
                (0x141, 4, 64, ValueError), (0x140, 2, 64, ValueError),
                (0x140, 4, True, ValueError)):
            before = d.snapshot()
            with self.assertRaises(error): d.write(offset, size, value)
            self.assertEqual(d.snapshot(), before)

    def test_bounded_history(self):
        d = self.delivery()
        for _ in range(50): d.write(0x140, 4, 64)
        s = d.snapshot()["software"]
        self.assertEqual(s["writes"], 50)
        self.assertEqual(len(s["recent"]), 32)
        self.assertEqual(s["sources"][0]["posts"], 50)

    def test_unmodeled_vpe_masks_are_not_silently_ignored(self):
        d = self.delivery()
        for offset in (0x1b0, 0x1b4, 0x1b8):
            before = d.snapshot()
            with self.assertRaises(NotImplementedError): d.write(offset, 4, 1)
            self.assertEqual(d.snapshot(), before)
