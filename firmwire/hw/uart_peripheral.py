"""Reusable FirmWire MMIO shell; register ABI and transport stay injectable."""
from .peripheral import FirmWirePeripheral
from .uart import UARTRegisterBank


class UARTPeripheral(FirmWirePeripheral):
    def __init__(self, name, address, size, register_bank=None, **kwargs):
        super().__init__(name, address, size, **kwargs)
        self.registers = register_bank if register_bank is not None else UARTRegisterBank()

    def hw_read(self, offset, size):
        return self.registers.read(offset, size)

    def hw_write(self, offset, size, value):
        # A full FIFO is a UART overflow, not an unassigned bus transaction.
        self.registers.write(offset, size, value)
        return True
