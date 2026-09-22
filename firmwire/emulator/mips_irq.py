"""Explicit native MIPS CPU interrupt input, not a platform interrupt controller.

Call on the emulator thread only, after CPU initialization. One platform
controller owns each pin and aggregates sources before using this endpoint.
No direct CPU struct casts, CP0 patches, firmware PCs or assumed VPE topology.
"""
import ctypes
import threading


class DeferredIRQOutputs:
    """Level handoff from a peripheral thread to an emulator-thread callback.

    Producers only update desired levels. The board must call flush() on the
    emulator thread. This is for persistent levels, not pulse delivery.
    """
    def __init__(self, endpoints):
        self.endpoints = tuple(endpoints)
        if not self.endpoints or not all(callable(sink) for sink in self.endpoints):
            raise ValueError("explicit IRQ endpoints required")
        self._lock = threading.Lock()
        self.desired = [False] * len(self.endpoints)
        self.driven = [False] * len(self.endpoints)
        self.transitions = [0] * len(self.endpoints)
        self._dirty = False

    def input(self, output):
        if type(output) is not int or not 0 <= output < len(self.endpoints):
            raise ValueError("invalid IRQ output")
        def set_level(level):
            if type(level) is not bool: raise ValueError("IRQ level must be boolean")
            with self._lock:
                self.desired[output] = level
                self._dirty = True
        return set_level

    def flush(self):
        if not self._dirty: return
        with self._lock:
            levels = list(self.desired)
            self._dirty = False
        for index, level in enumerate(levels):
            if self.driven[index] != level:
                self.endpoints[index](level)
                with self._lock:
                    self.driven[index] = level
                    self.transitions[index] += 1

    def snapshot(self):
        with self._lock:
            return dict(desired=list(self.desired), driven=list(self.driven),
                        transitions=list(self.transitions), mode="emulator-thread-level-handoff")


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
