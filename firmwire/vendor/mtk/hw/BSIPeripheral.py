"""Relocatable adapter for explicit digital BSI observation/pending modes."""
from firmwire.hw.peripheral import PassthroughPeripheral
from .bsi import BsiImmediateControl


class BSIImmediatePeripheral(PassthroughPeripheral):
    def __init__(self, name, address, size, bsi_mode="observe", **kwargs):
        super().__init__(name, address, size, **kwargs)
        self.control = BsiImmediateControl(size=size, mode=bsi_mode)
        self.log.warning("BSI %s analysis: no RF/DSP backend, no automatic completion", bsi_mode)

    def hw_read(self, offset, size):
        return self.control.read(offset, size)

    def hw_write(self, offset, size, value):
        return self.control.write(offset, size, value)

    def enable_control_observer(self):
        pass  # Already bounded to 64 events; never changes peripheral behavior.

    def control_observation(self):
        return self.control.facts()
