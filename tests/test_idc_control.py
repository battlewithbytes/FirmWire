import pickle
import unittest
from unittest.mock import Mock
from firmwire.vendor.mtk.hw.idc_control import IdleIDCCounter, MTKIDCControlPeripheral
from firmwire.vendor.mtk.loader import MTKLoader
from firmwire.vendor.mtk.machine import MT6878Machine


class IDCCounterTests(unittest.TestCase):
    def test_enable_does_not_invent_transmission(self):
        model = IdleIDCCounter()
        self.assertEqual(model.read(0x10,4),0)
        for _ in range(3):
            model.write(0xc,4,1)
            for _ in range(50): self.assertEqual(model.read(0x10,4),0)
        facts = model.facts()
        self.assertTrue(facts["enabled"])
        self.assertEqual(facts["enable_writes"],3)
        self.assertFalse(facts["scheduler_supported"])
        self.assertFalse(facts["completion_fabricated"])
        self.assertEqual(pickle.loads(pickle.dumps(model)).facts(),facts)
        model.reset()
        self.assertEqual(model.facts(),IdleIDCCounter().facts())

    def test_unreviewed_commands_and_scheduler_registers_fail(self):
        for offset in (0,4,8,0x14,0x100,0x200,0x300,0xffc):
            model = IdleIDCCounter()
            with self.assertRaises(NotImplementedError): model.write(offset,4,1)
            with self.assertRaises(NotImplementedError): model.read(offset,4)
            self.assertFalse(model.enabled)
        for value in (0,2,3,0xffffffff):
            with self.assertRaises(NotImplementedError): IdleIDCCounter().write(0xc,4,value)
        with self.assertRaises(NotImplementedError): IdleIDCCounter().read(0xc,4)

    def test_width_alignment_and_type_validation(self):
        for offset,size in ((0xc,1),(0xc,2),(0xc,8),(0xd,4),(-4,4),(False,4)):
            with self.assertRaises(ValueError): IdleIDCCounter().write(offset,size,1)
            with self.assertRaises(ValueError): IdleIDCCounter().read(offset,size)
        for value in (True,-1,2**32,1.0):
            with self.assertRaises(ValueError): IdleIDCCounter().write(0xc,4,value)

    def test_relocatable_adapter_and_copied_observations(self):
        devices = [MTKIDCControlPeripheral("idc",base,0x1000,
                   firmwire_machine=object.__new__(MT6878Machine)) for base in (0x400000,0x800000)]
        devices[0].hw_write(0xc,4,1)
        self.assertFalse(devices[1].control_observation()["enabled"])
        self.assertEqual(devices[0].hw_read(0x10,4),0)
        with self.assertRaises(NotImplementedError): devices[0].hw_write(0x200,4,7)
        facts = devices[0].control_observation()
        facts["last_unsupported"]["offset"] = 0
        self.assertEqual(devices[0].control_observation()["last_unsupported"]["offset"],0x200)

    def test_loader_explicit_selection_and_no_implicit_uart(self):
        for abi in ("disabled","mt6768-counter"):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"idc_control":abi}, "native"
            loader.capability_report = {}
            loader.add_memory_range, loader.create_peripheral, loader.write_capability_report = Mock(), Mock(), Mock()
            loader.build_peripheral_maps()
            mappings = {c.args[0]:c.kwargs for c in loader.add_memory_range.call_args_list}
            self.assertEqual(0xa60a0000 in mappings, abi != "disabled")
            self.assertNotIn(0xa60b0000,mappings)
            if abi != "disabled": self.assertIs(mappings[0xa60a0000]["emulate"],MTKIDCControlPeripheral)
        for abi,mode in (("unknown","native"),("mt6768-counter","rehosted")):
            loader = object.__new__(MTKLoader)
            loader.loader_args, loader.boot_mode = {"idc_control":abi}, mode
            loader.add_memory_range = Mock()
            with self.assertRaises(ValueError): loader.build_peripheral_maps()
            loader.add_memory_range.assert_not_called()
