"""Reviewed IDC UART control subset; no connectivity peer or interrupt routing."""
from firmwire.hw.uart import UARTCore, UARTRegisterBank
from firmwire.hw.uart_peripheral import UARTPeripheral


class MTKIDCUARTRegisters(UARTRegisterBank):
    # Baud configuration latches. No claim of cycle-accurate serial timing.
    BAUD_REGISTERS = (0x24, 0x28, 0x2c, 0x54, 0x58, 0x84, 0x88)
    # Reviewed IDC family layout: PRI, PRI_BITEN, PAT, PAT_BITEN, four slots.
    # Configuration only. No RX pattern matcher or separate PM interrupt yet.
    PATTERN_REGISTERS = tuple(0xc0 + slot*0x10 + field
                              for slot in range(4) for field in (0, 4, 8, 12))

    def __init__(self):
        super().__init__(UARTCore(fifo_depth=32), stride=4, access_sizes=(1, 2, 4),
                         fifo_triggers=(1, 8, 16, 24))

    def reset(self):
        super().reset()
        self.extra = {offset: 0 for offset in self.BAUD_REGISTERS}
        self.pattern_config = {offset: 0 for offset in self.PATTERN_REGISTERS}
        self.rx_threshold = 1

    def _check_extra(self, offset, size):
        if size not in self.access_sizes or offset % 4:
            raise ValueError("unsupported IDC UART access")

    def read(self, offset, size):
        self._check_extra(offset, size)
        if offset in self.pattern_config:
            return self.pattern_config[offset]
        if offset in self.extra:
            return self.extra[offset]
        if offset == 0x50:
            return self.rx_threshold
        if offset == 0x5c:
            return self.fcr
        return super().read(offset, size)

    def write(self, offset, size, value):
        self._check_extra(offset, size)
        if offset in self.pattern_config:
            # Driver parameters/readback are bytes even with word stores.
            # Reject unreviewed high bits instead of silently dropping them.
            if type(value) is not int or not 0 <= value <= 0xff:
                raise ValueError("unsupported IDC pattern configuration bits")
            self.pattern_config[offset] = value
            return True
        if offset in self.extra:
            self.extra[offset] = value & 255
            return True
        if offset == 0x50:
            if not 1 <= value <= self.core.fifo_depth:
                raise ValueError("unsupported IDC RX threshold")
            self.rx_threshold = value
            self.core.rx_trigger = value if self.core.fifo_enabled else 1
            self.core._update_irq()
            return True
        if offset == 8:
            # IDC has an explicit RXTRIG register, unlike a plain 16550.
            self.fifo_triggers = (self.rx_threshold,) * 4
        return super().write(offset, size, value)


class MTKIDCUARTPeripheral(UARTPeripheral):
    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, register_bank=MTKIDCUARTRegisters(), **kwargs)
