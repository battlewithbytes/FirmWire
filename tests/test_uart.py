"""Vendor-neutral UART tests, deliberately without modem fixtures."""
import unittest
import pickle
from unittest.mock import Mock
from firmwire.hw.uart import UARTCore, UARTRegisterBank
from firmwire.vendor.mtk.hw.idc_uart import MTKIDCUARTRegisters


class UARTTests(unittest.TestCase):
    def test_non_8250_frontend_composes_without_inheriting_register_abi(self):
        from firmwire.hw.uart_peripheral import UARTPeripheral
        from firmwire.vendor.mtk.machine import MT6878Machine

        class ExampleNon8250:
            # Illustrative ABI only; deliberately different data/status offsets.
            def __init__(self):
                self.core = UARTCore(2)

            def read(self, offset, size):
                if size == 4 and offset == 0x90: return self.core.read_rx()
                if size == 4 and offset == 0x94: return self.core.read_lsr()
                raise NotImplementedError("unknown example read")

            def write(self, offset, size, value):
                if size == 4 and offset == 0x90: return self.core.write_tx(value)
                raise NotImplementedError("unknown example write")

        bank = ExampleNon8250()
        device = UARTPeripheral("example", 0x500000, 0x100, register_bank=bank,
                                firmwire_machine=object.__new__(MT6878Machine))
        self.assertTrue(device.hw_write(0x90, 4, ord("k")))
        self.assertEqual(bank.core.drain_tx(), b"k")
        bank.core.receive(b"r")
        self.assertTrue(device.hw_read(0x94, 4) & 1)
        self.assertEqual(device.hw_read(0x90, 4), ord("r"))

    def test_other_vendor_extension_reuses_shell_and_core(self):
        from firmwire.hw.uart_peripheral import UARTPeripheral, UARTAccessError
        from firmwire.vendor.mtk.machine import MT6878Machine

        class ExampleRegisters(UARTRegisterBank):
            # Test-only ABI: not an MTK register or a claim about other silicon.
            def reset(self):
                super().reset()
                self.config = 0

            def read(self, offset, size):
                if offset == 0x40 and size == 1:
                    return self.config
                return super().read(offset, size)

            def write(self, offset, size, value):
                if offset == 0x40 and size == 1:
                    if value not in (0, 1):
                        raise ValueError("unsupported example config")
                    self.config = value
                    return True
                return super().write(offset, size, value)

        banks = [ExampleRegisters() for _ in range(2)]
        devices = [UARTPeripheral("example", address, 0x100, register_bank=bank,
                   firmwire_machine=object.__new__(MT6878Machine))
                   for address, bank in zip((0x300000, 0x500000), banks)]
        devices[0].hw_write(0x40, 1, 1)
        self.assertEqual([d.hw_read(0x40, 1) for d in devices], [1, 0])
        devices[0].hw_write(0, 1, ord("q"))
        self.assertEqual(banks[0].core.drain_tx(), b"q")
        self.assertEqual(banks[1].core.drain_tx(), b"")
        for offset, size, value in ((0x40, 4, 1), (0x40, 1, 2), (0x44, 1, 0)):
            with self.assertRaises(UARTAccessError): devices[0].hw_write(offset, size, value)
        banks[0].reset()
        self.assertEqual(devices[0].hw_read(0x40, 1), 0)

    def test_bank_rejections_keep_bus_context_without_payload(self):
        from firmwire.hw.uart_peripheral import UARTPeripheral, UARTAccessError
        from firmwire.vendor.mtk.machine import MT6878Machine
        for bank in (UARTRegisterBank(), MTKIDCUARTRegisters()):
            device = UARTPeripheral("test", 0x400000, 0x1000, register_bank=bank,
                                    firmwire_machine=object.__new__(MT6878Machine))
            for direction in ("read", "write"):
                with self.assertRaises(UARTAccessError) as caught:
                    if direction == "write": device.hw_write(0x100, 4, 0xdecafbad)
                    else: device.hw_read(0x100, 4)
                error = caught.exception
                self.assertEqual((error.direction, error.offset, error.size), (direction, 0x100, 4))
                self.assertNotIn("decafbad", str(error))
                self.assertIsInstance(error.__cause__, ValueError)

    def test_generic_peripheral_accepts_composed_register_bank(self):
        from firmwire.hw.uart_peripheral import UARTPeripheral
        from firmwire.vendor.mtk.machine import MT6878Machine
        levels = []
        bank = UARTRegisterBank(UARTCore(8, levels.append), stride=2,
                                access_sizes=(2,), fifo_triggers=(1,2,4,8))
        peripheral = UARTPeripheral("generic", 0x800000, 0x100,
                                    register_bank=bank, firmwire_machine=object.__new__(MT6878Machine))
        self.assertTrue(peripheral.hw_write(0,2,65))
        self.assertTrue(peripheral.hw_write(0,2,66))  # full FIFO is still mapped MMIO
        self.assertEqual(bank.core.tx_overflows, 1)
        self.assertEqual(bank.core.drain_tx(), b"A")
        peripheral.hw_write(2,2,1)
        bank.core.receive(b"Z")
        self.assertEqual(levels, [True])
        self.assertEqual(peripheral.hw_read(0,2), ord("Z"))
        self.assertEqual(levels, [True,False])

    def test_loader_selection_and_relocatable_independent_devices(self):
        from firmwire.vendor.mtk.loader import MTKLoader
        from firmwire.vendor.mtk.machine import MT6878Machine
        from firmwire.vendor.mtk.hw.idc_uart import MTKIDCUARTPeripheral
        for abi in ("disabled", "mt6768-control"):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"idc_uart": abi}, "native"
            loader.capability_report = {}
            loader.add_memory_range = Mock()
            loader.create_peripheral = Mock()
            loader.write_capability_report = Mock()
            loader.build_peripheral_maps()
            mappings = [c for c in loader.add_memory_range.call_args_list if c.args[0] == 0xa60b0000]
            self.assertEqual(len(mappings), int(abi != "disabled"))
            if mappings:
                self.assertIs(mappings[0].kwargs["emulate"], MTKIDCUARTPeripheral)
                self.assertFalse(loader.capability_report["idc_uart"]["guest_irq_routed"])
                self.assertTrue(loader.capability_report["idc_uart"]["pattern_configuration_supported"])
                self.assertFalse(loader.capability_report["idc_uart"]["pattern_matching_supported"])
        for abi, mode in (("unknown", "native"), ("mt6768-control", "rehosted")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"idc_uart": abi}, mode
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError): loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()
        devices = [MTKIDCUARTPeripheral("uart", address, 0x1000,
                   firmwire_machine=object.__new__(MT6878Machine)) for address in (0x400000, 0x600000)]
        devices[0].hw_write(0x54, 4, 0x75)
        self.assertEqual(devices[1].hw_read(0x54, 4), 0)
        restored = pickle.loads(pickle.dumps(devices[0].registers))
        self.assertEqual(restored.read(0x54,4), 0x75)

    def test_layouts_and_divisor_aliases(self):
        for stride in (1, 2, 4):
            for size in (s for s in (1, 2, 4) if s <= stride):
                bank = UARTRegisterBank(stride=stride, access_sizes=(size,))
                bank.write(3*stride, size, 0x83)
                bank.write(0, size, 0x12)
                bank.write(stride, size, 0x34)
                self.assertEqual(bank.read(0, size), 0x12)
                self.assertEqual(bank.read(stride, size), 0x34)
                self.assertEqual(bank.core.drain_tx(), b"")
                bank.write(3*stride, size, 3)
                self.assertEqual(bank.read(stride, size), 0)
                bank.write(0, size, 0x56)
                self.assertEqual(bank.core.drain_tx(), b"V")
                self.assertEqual(bank.read(0, size), 0)

    def test_fifo_capacity_overflow_and_reset(self):
        for depth in (1, 2, 16, 32, 64):
            core = UARTCore(depth)
            core.configure_fifo(True)
            self.assertEqual(core.receive(bytes(range(depth+1))), depth)
            self.assertTrue(core.read_lsr() & 2)
            self.assertFalse(core.read_lsr() & 2)
            self.assertEqual(bytes(core.read_rx() for _ in range(depth)), bytes(range(depth)))
            for byte in range(depth):
                self.assertTrue(core.write_tx(byte))
            self.assertFalse(core.write_tx(255))
            self.assertEqual(core.drain_tx(), bytes(range(depth)))
            core.reset()
            self.assertEqual(core.capacity, 1)
            self.assertEqual(core.read_lsr(), 0x60)

    def test_interrupt_priority_acknowledgement_and_sink(self):
        levels = []
        core = UARTCore(4, levels.append)
        core.set_ier(7)
        self.assertEqual(core.read_iir(), 2)
        self.assertEqual(levels, [True, False])
        core.receive(b"ab")
        self.assertEqual(core.read_iir(), 6)
        core.read_lsr()
        self.assertEqual(core.read_iir(), 4)
        self.assertEqual(core.read_rx(), ord("a"))
        self.assertFalse(core.irq)
        core.write_tx(7)
        core.drain_tx()
        self.assertTrue(core.irq)
        core.reset()
        self.assertFalse(core.irq)
        self.assertFalse(levels[-1])

    def test_threshold_and_fcr_clear(self):
        bank = UARTRegisterBank()
        bank.write(2, 1, 0x41)
        bank.write(1, 1, 1)
        bank.core.receive(b"abc")
        self.assertFalse(bank.core.irq)
        bank.core.receive(b"d")
        self.assertTrue(bank.core.irq)
        bank.write(2, 1, 0x43)
        self.assertFalse(bank.core.irq)
        self.assertEqual(bank.fcr, 0x41)

    def test_invalid_accesses_and_unsupported_features(self):
        bank = UARTRegisterBank(stride=4, access_sizes=(4,))
        for offset, size in ((1,4), (0,8), (-4,4), (32,4)):
            with self.assertRaises(ValueError): bank.read(offset, size)
        for offset, value in ((4,8), (8,8), (16,16), (20,1)):
            with self.assertRaises(ValueError): bank.write(offset, 4, value)

    def test_idc_configuration_no_implicit_peer(self):
        bank = MTKIDCUARTRegisters()
        # Independent transcription of the observed driver configuration.
        for offset, value in ((0x24,3),(0xc,0x80),(0,1),(4,0),
                              (0x28,5),(0x2c,2),(0x54,0x75),(0x84,0x75),
                              (0x58,0),(0x88,0),(0xc,3),(0x50,1),(8,0xc7),(4,1)):
            bank.write(offset, 4, value)
        self.assertEqual((bank.dll, bank.dlm, bank.lcr), (1,0,3))
        self.assertEqual(bank.read(0x5c,4), 0xc1)
        self.assertEqual(bank.read(0x14,4), 0x60)
        self.assertEqual(bank.read(8,4), 0xc1)
        self.assertFalse(bank.core.irq)
        self.assertEqual(bank.core.drain_tx(), b"")
        bank.core.receive(b"x")
        self.assertTrue(bank.core.irq)
        self.assertEqual(bank.read(0,4), ord("x"))
        self.assertFalse(bank.core.irq)
        for offset in (0x20, 0xbc, 0x100):
            with self.assertRaises(ValueError): bank.read(offset,4)
        bank.reset()
        self.assertEqual(bank.read(0x54,4), 0)

    def test_idc_pattern_configuration_all_slots_widths_and_reset(self):
        # Independent family ABI transcription; no firmware PC, image or fixture.
        for size in (1, 2, 4):
            bank, other = MTKIDCUARTRegisters(), MTKIDCUARTRegisters()
            levels = []
            bank.core.irq_sink = levels.append
            expected = {}
            for slot in range(4):
                base = 0xc0 + 16*slot
                bank.write(base+4, size, 0)  # driver disables matching while configuring
                for field, value in ((8, 0xa0+slot), (12, 0xf0-slot), (0, 0x30+slot), (4, 0x70+slot)):
                    bank.write(base+field, size, value)
                    expected[base+field] = value
            for offset, value in expected.items():
                self.assertEqual(bank.read(offset, size), value)
                self.assertEqual(other.read(offset, size), 0)
            restored = pickle.loads(pickle.dumps(bank))
            for offset, value in expected.items(): self.assertEqual(restored.read(offset, size), value)
            self.assertEqual(levels, [])
            self.assertEqual(bank.core.drain_tx(), b"")
            self.assertEqual(list(bank.core.rx), [])
            bank.core.receive(b"R")
            bank.core.write_tx(ord("T"))
            bank.core.set_ier(1)
            before = (list(bank.core.rx), list(bank.core.tx), bank.core.irq, list(levels))
            bank.write(0xc4, size, 0)
            bank.write(0xcc, size, 0xff)
            self.assertEqual((list(bank.core.rx), list(bank.core.tx), bank.core.irq, levels), before)
            bank.reset()
            for offset in expected: self.assertEqual(bank.read(offset, size), 0)

    def test_idc_pattern_configuration_rejects_unknown_and_reserved_values(self):
        bank = MTKIDCUARTRegisters()
        bank.write(0xc4, 4, 0x5a)
        for value in (256, 0x10000, -1, True, 1.0):
            with self.assertRaises(ValueError): bank.write(0xc4, 4, value)
            self.assertEqual(bank.read(0xc4, 4), 0x5a)
        for offset, size in ((0xc1, 1), (0xc4, 8), (0xbc, 4), (0x100, 4)):
            with self.assertRaises(ValueError): bank.read(offset, size)
            with self.assertRaises(ValueError): bank.write(offset, size, 0)
