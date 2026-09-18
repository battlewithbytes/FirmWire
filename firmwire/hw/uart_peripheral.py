"""Reusable FirmWire MMIO shell; register ABI and transport stay injectable."""
from .peripheral import FirmWirePeripheral
from .uart import UARTRegisterBank


class UARTAccessError(ValueError):
    """Register-bank rejection with payload-free bus context."""
    def __init__(self, direction, offset, size):
        self.direction, self.offset, self.size = direction, offset, size
        super().__init__(f"unsupported UART {direction}: offset={offset:#x} size={size}")


class UARTPeripheral(FirmWirePeripheral):
    def __init__(self, name, address, size, register_bank=None, **kwargs):
        super().__init__(name, address, size, **kwargs)
        self.registers = register_bank if register_bank is not None else UARTRegisterBank()

    def hw_read(self, offset, size):
        try:
            return self.registers.read(offset, size)
        except (ValueError, NotImplementedError) as error:
            raise UARTAccessError("read", offset, size) from error

    def hw_write(self, offset, size, value):
        # A full FIFO is a UART overflow, not an unassigned bus transaction.
        try:
            self.registers.write(offset, size, value)
        except (ValueError, NotImplementedError) as error:
            raise UARTAccessError("write", offset, size) from error
        return True
