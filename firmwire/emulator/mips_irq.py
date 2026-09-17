"""Explicit native MIPS CPU interrupt input, not a platform interrupt controller.

Call on the emulator thread only, after CPU initialization. One platform
controller owns each pin and aggregates sources before using this endpoint.
No direct CPU struct casts, CP0 patches, firmware PCs or assumed VPE topology.
"""
import ctypes


class MipsIRQInput:
    def __init__(self, panda, cpu_index, pin):
        if type(cpu_index) is not int or not 0 <= cpu_index <= 0x7fffffff:
            raise ValueError("cpu_index must fit a nonnegative C int")
        if type(pin) is not int or not 2 <= pin <= 7:
            raise ValueError("pin must be a MIPS hardware input (2..7)")
        self.cpu_index, self.pin = cpu_index, pin
        self._library = ctypes.CDLL(panda.libpanda_path)
        try:
            self._set_irq = self._library.configurable_mips_set_irq
        except AttributeError as error:
            raise RuntimeError("engine lacks compiled MIPS IRQ endpoint") from error
        self._set_irq.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
        self._set_irq.restype = ctypes.c_int

    def __call__(self, level):
        if type(level) is not bool:
            raise ValueError("IRQ level must be boolean")
        result = self._set_irq(self.cpu_index, self.pin, int(level))
        if result != 0:
            raise RuntimeError("MIPS IRQ endpoint rejected input: %d" % result)
