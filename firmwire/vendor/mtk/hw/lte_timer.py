"""93xx LTE timer RR-value storage, not a running timer or interrupt source.

Set/Dump RR Value access two four-word banks with stride 0x14. Trigger RR
Event uses the preceding words (+0x58/+0x6c), intentionally NOT accepted.
See docs/lte-timer.md for exact-ROM and sibling-symbol evidence.
"""
import json

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.peripheral import FirmWirePeripheral


class MTKLTETimerRRPeripheral(FirmWirePeripheral):
    RR_OFFSETS = tuple(0x5c + bank * 0x14 + word * 4 for bank in range(2) for word in range(4))

    def __init__(self, name, address, size, **kwargs):
        if size < 0x80:
            raise ValueError("LTE RR configuration window is too small")
        super().__init__(name, address, size, **kwargs)
        self.config = ConfigurationRegisters(self.RR_OFFSETS)
        self._observe_control = False

    def hw_read(self, offset, size):
        try:
            return self.config.read(offset, size)
        except NotImplementedError:
            self._record_stop()
            raise

    def hw_write(self, offset, size, value):
        try:
            return self.config.write(offset, size, value)
        except NotImplementedError:
            self._record_stop()
            raise

    def enable_control_observer(self):
        self._observe_control = True

    def _record_stop(self):
        # A failed forwarded MMIO stalls before the next block callback. Keep
        # this final snapshot in the log, not a stale periodic report. Do not
        # read guest RAM or call QEMU synchronously from its MMIO worker.
        if self._observe_control:
            self.log.error("LTE RR unsupported metadata=%s",
                           json.dumps(self.control_observation(), sort_keys=True))

    def control_observation(self):
        return dict(self.config.facts(), kind="93xx-lte-rr-configuration/v1",
                    configuration_only=True, clock_supported=False,
                    rr_trigger_supported=False, guest_irq_routed=False,
                    expiry_fabricated=False, boot_verified=False)
