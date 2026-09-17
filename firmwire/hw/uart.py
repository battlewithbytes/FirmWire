"""Deterministic UART building blocks, independent of vendor, CPU and firmware.

UARTCore models byte queues and level interrupt state. UARTRegisterBank is a
deliberately limited 8250-style register frontend, not a complete 16550 model.
No host I/O, implicit peer, baud timing, DMA, or CPU interrupt routing lives here.
Backends explicitly drain TX and inject RX on the emulator thread.
"""
from collections import deque


class UARTCore:
    def __init__(self, fifo_depth=16, irq_sink=None):
        if type(fifo_depth) is not int or fifo_depth < 1:
            raise ValueError("fifo_depth must be a positive integer")
        self.fifo_depth = fifo_depth
        self.irq_sink = irq_sink
        self.irq = False
        self.reset()

    def reset(self):
        self.rx, self.tx = deque(), deque()
        self.fifo_enabled = False
        self.ier = 0
        self.rx_trigger = 1
        self.overrun = False
        self.tx_pending = True
        self.tx_overflows = 0
        self._update_irq()

    @property
    def capacity(self):
        return self.fifo_depth if self.fifo_enabled else 1

    def interrupt_id(self):
        if self.ier & 4 and self.overrun:
            return 6
        if self.ier & 1 and len(self.rx) >= self.rx_trigger:
            return 4
        if self.ier & 2 and self.tx_pending:
            return 2
        return 1

    def _update_irq(self):
        level = self.interrupt_id() != 1
        if level != self.irq:
            self.irq = level
            if self.irq_sink is not None:
                self.irq_sink(level)

    def set_ier(self, value):
        if value & ~7:
            raise ValueError("unsupported UART interrupt enable bits")
        if value & 2 and not self.ier & 2 and not self.tx:
            self.tx_pending = True
        self.ier = value
        self._update_irq()

    def configure_fifo(self, enabled, clear_rx=False, clear_tx=False):
        if enabled != self.fifo_enabled:
            clear_rx = clear_tx = True
        self.fifo_enabled = enabled
        if clear_rx:
            self.rx.clear()
            self.overrun = False
        if clear_tx:
            self.tx.clear()
            self.tx_pending = True
        self._update_irq()

    def receive(self, data):
        accepted = 0
        for byte in bytes(data):
            if len(self.rx) == self.capacity:
                self.overrun = True
            else:
                self.rx.append(byte)
                accepted += 1
        self._update_irq()
        return accepted

    def read_rx(self):
        value = self.rx.popleft() if self.rx else 0
        self._update_irq()
        return value

    def write_tx(self, byte):
        if len(self.tx) == self.capacity:
            self.tx_overflows += 1
            return False
        self.tx.append(byte & 255)
        self.tx_pending = False
        self._update_irq()
        return True

    def drain_tx(self, count=None):
        if count is None:
            count = len(self.tx)
        if type(count) is not int or count < 0:
            raise ValueError("count must be nonnegative")
        data = bytes(self.tx.popleft() for _ in range(min(count, len(self.tx))))
        if data and not self.tx:
            self.tx_pending = True
        self._update_irq()
        return data

    def read_iir(self):
        value = self.interrupt_id()
        if value == 2:
            self.tx_pending = False
        self._update_irq()
        return value

    def read_lsr(self):
        value = bool(self.rx) | (2 if self.overrun else 0) | (0x60 if not self.tx else 0)
        self.overrun = False
        self._update_irq()
        return value


class UARTRegisterBank:
    def __init__(self, core=None, stride=1, access_sizes=(1,), fifo_triggers=(1, 4, 8, 14)):
        self.core = core if core is not None else UARTCore()
        if type(stride) is not int or stride < 1:
            raise ValueError("stride must be positive")
        if not access_sizes or any(s not in (1, 2, 4) or s > stride for s in access_sizes):
            raise ValueError("access sizes must fit one register slot")
        if len(fifo_triggers) != 4 or any(type(n) is not int or not 1 <= n <= self.core.fifo_depth for n in fifo_triggers):
            raise ValueError("four valid FIFO thresholds required")
        self.stride, self.access_sizes = stride, tuple(access_sizes)
        self.fifo_triggers = tuple(fifo_triggers)
        self.reset()

    def reset(self):
        self.core.reset()
        self.lcr = self.mcr = self.dll = self.dlm = self.scratch = self.fcr = 0

    def _index(self, offset, size):
        if size not in self.access_sizes or offset < 0 or offset % self.stride or offset // self.stride > 7:
            raise ValueError("unsupported UART register access")
        return offset // self.stride

    def read(self, offset, size):
        reg = self._index(offset, size)
        if reg == 0:
            return self.dll if self.lcr & 0x80 else self.core.read_rx()
        if reg == 1:
            return self.dlm if self.lcr & 0x80 else self.core.ier
        if reg == 2:
            return self.core.read_iir() | (0xc0 if self.core.fifo_enabled else 0)
        if reg == 5:
            return self.core.read_lsr()
        return {3: self.lcr, 4: self.mcr, 6: 0, 7: self.scratch}[reg]

    def write(self, offset, size, value):
        reg = self._index(offset, size)
        value &= 255
        if reg == 0:
            if self.lcr & 0x80:
                self.dll = value
            else:
                return self.core.write_tx(value)
        elif reg == 1:
            if self.lcr & 0x80:
                self.dlm = value
            else:
                self.core.set_ier(value)
        elif reg == 2:
            if value & 0x38:
                raise ValueError("unsupported UART FCR bits")
            self.core.rx_trigger = self.fifo_triggers[value >> 6] if value & 1 else 1
            self.core.configure_fifo(bool(value & 1), bool(value & 2), bool(value & 4))
            self.fcr = value & ~6
        elif reg == 3:
            self.lcr = value
        elif reg == 4:
            if value & ~0xf:
                raise ValueError("UART loopback/flow control not implemented")
            self.mcr = value
        elif reg == 7:
            self.scratch = value
        else:
            raise ValueError("read-only UART register")
        return True
