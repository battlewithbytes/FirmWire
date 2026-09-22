import unittest

from firmwire.vendor.mtk.hw.mdcirq import MdcirqNormalIRQBank, NORMAL_IRQ_LAYOUT


class MdcirqBankTests(unittest.TestCase):
    def make(self, outputs=4, shift=0, inclusive=False):
        self.edges = [[] for _ in range(outputs)]
        bank = MdcirqNormalIRQBank([edge.append for edge in self.edges],
            minimum_inclusive=inclusive, source_count=8,
            layout={name: offset+shift for name, offset in NORMAL_IRQ_LAYOUT.items()})
        return bank

    def test_mask_aliases_group_polarity_and_restore_previous_id(self):
        for count in (1, 2, 4):
            for shift in (0, 0x1000):
                bank = self.make(count, shift)
                put = lambda offset, value: bank.write(offset+shift, value)
                get = lambda offset: bank.read(offset+shift)
                put(0x300, 0x04030201)
                put(0x600, ((1 << count) - 1) ^ (1 << (count-1)))
                put(0x40, 1 << 2)
                self.assertFalse(get(0x20) & 4)
                bank.set_level(2, True)
                self.assertEqual(self.edges[count-1], [True])
                self.assertEqual(get(0xc20+(count-1)*4), 2)
                self.assertEqual(get(0x220+(count-1)*4), 3)
                with self.assertRaises(ValueError): put(0xc70+(count-1)*4, 2)
                bank.set_level(2, False)
                put(0xc70+(count-1)*4, 0xffff)
                self.assertEqual(get(0xc20+(count-1)*4), 0x1ff)
                put(0x60, 4)
                self.assertTrue(get(0x20) & 4)

    def test_nested_restore_validates_parent_not_current(self):
        bank = self.make(1)
        bank.write(0x300, 0x7f7f0102)
        bank.write(0x600, 0)
        bank.write(0x40, 3)
        bank.set_level(0, True)
        self.assertEqual(bank.read(0xc20), 0)
        bank.set_level(1, True)
        self.assertEqual(bank.read(0xc20), 1)
        before = bank.snapshot()
        with self.assertRaises(ValueError): bank.write(0xc70, 0xffff)
        self.assertEqual(bank.snapshot(), before)
        bank.set_level(1, False)
        bank.write(0xc70, 0)
        self.assertEqual(bank.read(0x200), 0)
        bank.set_level(0, False)
        bank.write(0xc70, 0xffff)

    def test_explicit_threshold_policy_and_state_gate(self):
        for inclusive in (True, False):
            bank = self.make(1, inclusive=inclusive)
            bank.write(0x600, 0)
            bank.write(0x40, 1)
            bank.set_level(0, True)
            self.assertEqual(bank.snapshot()["controller"]["output_levels"], [inclusive])
            bank.write(0x400, 0)
            self.assertEqual(bank.read(0xc20), 0x1ff)
            bank.write(0x400, 386)
            self.assertEqual(bank.snapshot()["controller"]["output_levels"], [inclusive])

    def test_unsupported_registers_and_bad_writes_do_not_mutate(self):
        bank = self.make()
        before = bank.snapshot()
        for offset, value in ((0x120, 1), (0x640, 1), (0x300, 0x80000000),
                              (0x500, 0x10000000), (0xc60, 1)):
            with self.assertRaises(NotImplementedError): bank.write(offset, value)
            self.assertEqual(bank.snapshot(), before)
        for offset, value in ((0x600, 16), (0x260, 128), (0x400, 512), (0x20, -1)):
            with self.assertRaises(ValueError): bank.write(offset, value)
            self.assertEqual(bank.snapshot(), before)
        with self.assertRaises(ValueError): bank.read(0xc21)
        with self.assertRaises(ValueError): bank.read(0xc20, 2)

    def test_reset_empty_stack_and_layout_validation(self):
        bank = self.make()
        for vpe in range(4):
            for _ in range(128): bank.write(0xc60+4*vpe, 0x1ff)
        self.assertEqual(bank.snapshot()["controller"]["active"], [[], [], [], []])
        overlap = dict(NORMAL_IRQ_LAYOUT, irq_return=NORMAL_IRQ_LAYOUT["irq_id"])
        with self.assertRaises(ValueError): MdcirqNormalIRQBank([lambda _: None], minimum_inclusive=False, layout=overlap)
        # More outputs need a non-overlapping register layout; never silently
        # alias return/priority banks to pretend the legacy page supports them.
        with self.assertRaises(ValueError): self.make(8)

    def test_relocated_layout_can_support_eight_outputs(self):
        layout = {name: index*0x100 for index, name in enumerate(NORMAL_IRQ_LAYOUT)}
        bank = MdcirqNormalIRQBank([lambda _: None]*8, minimum_inclusive=False, layout=layout)
        bank.write(layout["priority"], 0x01010101)
        bank.write(layout["route"], 0x7f)
        bank.write(layout["mask_clear"], 1)
        bank.set_level(0, True)
        self.assertEqual(bank.read(layout["irq_id"]+7*4), 0)
