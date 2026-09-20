"""Explicit analysis hypothesis, not a complete 93xx D2BIF implementation."""
import json

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.peripheral import FirmWirePeripheral


class MTKD2BIFStorageAnalysisPeripheral(FirmWirePeripheral):
    """Retain two guest-programmed words; do not fabricate data-plane effects.

    Initializers write these words and a diagnostic routine reads them. That
    evidence does NOT establish bit meanings, reset values or side effects.
    Both the retained-value behavior and absence of effects are provisional.
    No firmware identity, PC or expected initialization value selects behavior.
    """
    CONFIG_OFFSETS = (0x28, 0x4c)

    @classmethod
    def analysis_facts(cls):
        return dict(kind="93xx-d2bif-storage-analysis/v1", analysis_only=True,
                    hypothesis="d2bif-two-word-storage-no-effects/v1",
                    semantics_verified=False, boot_verified=False,
                    dma_supported=False, completion_fabricated=False, guest_irq_routed=False,
                    assumptions=["+0x28/+0x4c retain independent 32-bit writes",
                                 "reads return last write; reset values unknown",
                                 "no transfer, completion or interrupt side effects"])

    def __init__(self, name, address, size, **kwargs):
        if size < max(self.CONFIG_OFFSETS) + 4:
            raise ValueError("D2BIF configuration window is too small")
        super().__init__(name, address, size, **kwargs)
        self.config = ConfigurationRegisters(self.CONFIG_OFFSETS)
        self.recent_accesses = []
        self._observe_control = False
        self.log.warning("D2BIF PROVISIONAL ANALYSIS model=%s",
                         json.dumps(self.analysis_facts(), sort_keys=True))

    def enable_control_observer(self):
        self._observe_control = True

    def _record_stop(self):
        if self._observe_control:
            self.log.error("D2BIF analysis unsupported metadata=%s",
                           json.dumps(self.control_observation(), sort_keys=True))

    def _record_access(self, direction, offset, value):
        self.recent_accesses.append(dict(direction=direction, offset=offset, value=value))
        del self.recent_accesses[:-32]
        if self.config.reads + self.config.writes <= 32:
            self.log.warning("D2BIF analysis access metadata=%s",
                             json.dumps(self.control_observation(), sort_keys=True))

    def hw_read(self, offset, size):
        try:
            value = self.config.read(offset, size)
        except NotImplementedError:
            self._record_stop()
            raise
        self._record_access("read", offset, value)
        return value

    def hw_write(self, offset, size, value):
        try:
            result = self.config.write(offset, size, value)
        except NotImplementedError:
            self._record_stop()
            raise
        self._record_access("write", offset, value)
        return result

    def control_observation(self):
        return dict(self.config.facts(), **self.analysis_facts(),
                    recent_accesses=[dict(event) for event in self.recent_accesses])
