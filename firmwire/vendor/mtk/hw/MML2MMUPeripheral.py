"""Address-independent adapter for the opt-in 93xx MMU control model."""
from firmwire.hw.peripheral import PassthroughPeripheral
from .mml2_mmu import MML2MMU93Control


class MML2MMU93Peripheral(PassthroughPeripheral):
    def __init__(self, name, address, size, **kwargs):
        if size != MML2MMU93Control.SIZE:
            raise ValueError("93xx MML2 MMU control bank must be 0x1000 bytes")
        super().__init__(name, address, size, **kwargs)
        self.control = MML2MMU93Control()
        self.log.warning("MML2 MMU: opt-in 93xx control-only analysis; no translation or DMA")

    def hw_read(self, offset, size):
        return self.control.read(offset, size)

    def hw_write(self, offset, size, value):
        return self.control.write(offset, size, value)

    def control_facts(self):
        return self.control.facts()
