import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from firmwire.emulator.mips_irq import MipsIRQInput


class MipsIRQTests(unittest.TestCase):
    def test_inputs_never_wrap_or_guess_cpu(self):
        for cpu, pin in ((-1,5),(True,5),(2**32,5),(0,1),(0,8),(0,True)):
            with patch("firmwire.emulator.mips_irq.ctypes.CDLL") as load:
                with self.assertRaises(ValueError): MipsIRQInput(SimpleNamespace(libpanda_path="unused"),cpu,pin)
                load.assert_not_called()

    def test_explicit_destinations_levels_and_engine_refusal(self):
        for cpu in (0,1,3,7):
            function = Mock(return_value=0)
            library = SimpleNamespace(configurable_mips_set_irq=function)
            with patch("firmwire.emulator.mips_irq.ctypes.CDLL",return_value=library):
                irq = MipsIRQInput(SimpleNamespace(libpanda_path="matched-engine"),cpu,5)
                irq(True)
                function.assert_called_with(cpu,5,1)
                irq(False)
                function.assert_called_with(cpu,5,0)
                for invalid in (0,1,None):
                    with self.assertRaises(ValueError): irq(invalid)
                function.return_value = -2
                with self.assertRaises(RuntimeError): irq(True)

    def test_old_engine_is_not_silently_emulated(self):
        with patch("firmwire.emulator.mips_irq.ctypes.CDLL",return_value=SimpleNamespace()):
            with self.assertRaisesRegex(RuntimeError,"lacks compiled"):
                MipsIRQInput(SimpleNamespace(libpanda_path="old-engine"),0,5)
