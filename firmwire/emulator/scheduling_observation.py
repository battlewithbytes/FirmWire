"""Read-only scheduler diagnostics through compiled APIs, not CPU CFFI fields."""
import ctypes


class SchedulingObserver:
    def __init__(self, panda, cpu_count):
        if type(cpu_count) is not int or not 1 <= cpu_count <= 64:
            raise ValueError("explicit bounded realized CPU count required")
        # Compiled CFFI builds cannot accept additional cdef declarations.
        self.native = ctypes.CDLL(panda.libpanda_path)
        for name, args, result in (
                ("qemu_get_cpu", [ctypes.c_int], ctypes.c_void_p),
                ("cpu_is_stopped", [ctypes.c_void_p], ctypes.c_bool),
                ("qemu_clock_get_ns", [ctypes.c_int], ctypes.c_int64)):
            function = getattr(self.native, name)
            function.argtypes, function.restype = args, result
        self.read_pc = lambda cpu: panda.libpanda.panda_current_pc(panda.ffi.cast("CPUState *", cpu))
        self.cpus = []
        for index in range(cpu_count):
            cpu = self.native.qemu_get_cpu(index)
            if not cpu:
                raise ValueError("requested CPU is not realized")
            self.cpus.append(cpu)
        self.first = None
        self.recent = []
        self.samples = 0

    def sample(self, block):
        """Call on the emulator thread at an existing observation boundary.

        No monitor transaction, pause, kick, guest writes or virtual-clock
        enable call. `stopped` is QEMU/debugger state, NOT architectural halt.
        """
        row = dict(completed_blocks=block, virtual_ns=int(self.native.qemu_clock_get_ns(1)),
                   cpus=[dict(index=index, stopped=bool(self.native.cpu_is_stopped(cpu)),
                              pc=int(self.read_pc(cpu)))
                         for index, cpu in enumerate(self.cpus)])
        self.samples += 1
        if self.first is None:
            self.first = row
        self.recent.append(row)
        del self.recent[:-16]
        return dict(schema="firmwire.scheduling-observation/v1", read_only=True,
                    timing="emulator-thread checkpoint", stopped_semantics="QEMU/debugger, not architectural halt",
                    first=self.first, recent=list(self.recent), samples=self.samples,
                    firmware_boot_verified=False)
