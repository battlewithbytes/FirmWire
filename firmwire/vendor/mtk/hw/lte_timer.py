"""93xx LTE timer RR-value storage, not a running timer or interrupt source.

Set/Dump RR Value access two four-word banks with stride 0x14. Trigger RR
Event uses the preceding words (+0x58/+0x6c), intentionally NOT accepted.
See docs/lte-timer.md for exact-ROM and sibling-symbol evidence.
"""
from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.peripheral import FirmWirePeripheral


class MTKLTETimerRRPeripheral(FirmWirePeripheral):
    RR_OFFSETS = tuple(0x5c + bank * 0x14 + word * 4 for bank in range(2) for word in range(4))

    def __init__(self, name, address, size, **kwargs):
        if size < 0x80:
            raise ValueError("LTE RR configuration window is too small")
        super().__init__(name, address, size, **kwargs)
        self.config = ConfigurationRegisters(self.RR_OFFSETS)

    def hw_read(self, offset, size):
        return self.config.read(offset, size)

    def hw_write(self, offset, size, value):
        return self.config.write(offset, size, value)

    def enable_control_observer(self):
        pass

    def control_observation(self):
        return dict(self.config.facts(), kind="93xx-lte-rr-configuration/v1",
                    configuration_only=True, clock_supported=False,
                    rr_trigger_supported=False, guest_irq_routed=False,
                    expiry_fabricated=False, boot_verified=False)
