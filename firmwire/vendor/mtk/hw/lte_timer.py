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
    CONFIG_OFFSETS = RR_OFFSETS
    KIND = "93xx-lte-rr-configuration/v1"
    STOP_LABEL = "LTE RR unsupported metadata=%s"

    def __init__(self, name, address, size, **kwargs):
        if size < max(self.CONFIG_OFFSETS) + 4:
            raise ValueError("LTE configuration window is too small")
        super().__init__(name, address, size, **kwargs)
        self.config = ConfigurationRegisters(self.CONFIG_OFFSETS)
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
            self.log.error(self.STOP_LABEL,
                           json.dumps(self.control_observation(), sort_keys=True))

    def control_observation(self):
        return dict(self.config.facts(), kind=self.KIND,
                    configuration_only=True, clock_supported=False,
                    rr_trigger_supported=False, guest_irq_routed=False,
                    expiry_fabricated=False, boot_verified=False)


class MTKLTETimerControlPeripheral(MTKLTETimerRRPeripheral):
    """Reviewed configuration plane; commands/status still fail explicitly.

    Zero source-mask words disable that output; firmware restores its computed
    bitmap to unmask it. Storage does not generate pending events. The sixteen
    group offset parameters end BEFORE the group trigger registers at +0x1b98.
    Mode bits are retained, not interpreted as verified edge/level behavior.
    """
    SOURCE_MASK_OFFSETS = tuple(range(0x4a4, 0x4c4, 4))
    GROUP_OFFSET_REGS = tuple(range(0x1b58, 0x1b98, 4))
    CONFIG_OFFSETS = (MTKLTETimerRRPeripheral.RR_OFFSETS + (0x4a0,) +
                      SOURCE_MASK_OFFSETS + GROUP_OFFSET_REGS)
    KIND = "93xx-lte-control-configuration/v1"
    STOP_LABEL = "LTE control unsupported metadata=%s"

    def control_observation(self):
        return dict(super().control_observation(), source_mask_storage=True,
                    group_offset_storage=True, irq_mode_storage=True,
                    irq_status_supported=False, mode_semantics_verified=False)
