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


class MTKLTETimerInitStorageAnalysisPeripheral(MTKLTETimerControlPeripheral):
    """Provisional H0: two initialization words retain writes without effects.

    This is NOT a reviewed register contract. No enable/ack/selector meaning
    is assigned to individual bits. Arbitrary guest words are retained;
    unwritten reads, other registers and non-word accesses remain strict.
    CPU PCs, firmware identities and expected initialization values do not
    participate in this model. Selection must be explicit and analysis-only.
    """
    HYPOTHESIS_OFFSETS = (0x4ec, 0x4f0)
    CONFIG_OFFSETS = MTKLTETimerControlPeripheral.CONFIG_OFFSETS + HYPOTHESIS_OFFSETS
    KIND = "93xx-lte-init-storage-analysis/v1"
    STOP_LABEL = "LTE analysis unsupported metadata=%s"

    @classmethod
    def analysis_facts(cls):
        return dict(analysis_only=True, hypothesis="init-word-storage-no-effects/v1",
                    semantics_verified=False, boot_verified=False,
                    assumptions=["+0x4ec/+0x4f0 retain independent 32-bit writes",
                                 "reads return the last write; reset values unknown",
                                 "these writes have no timer or IRQ side effects"],
                    hypothesis_offsets=list(cls.HYPOTHESIS_OFFSETS))

    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, **kwargs)
        self.hypothesis_reads = self.hypothesis_writes = 0
        self.hypothesis_trace = []
        self.log.warning("LTE PROVISIONAL ANALYSIS model=%s",
                         json.dumps(self.analysis_facts(), sort_keys=True))

    def _trace(self, direction, offset, value):
        self.hypothesis_trace.append(dict(direction=direction, offset=offset, value=value))
        del self.hypothesis_trace[:-32]
        # Bounded logging even if firmware later polls these words indefinitely.
        if self.hypothesis_reads + self.hypothesis_writes <= 32:
            self.log.warning("LTE analysis access metadata=%s",
                             json.dumps(self.control_observation(), sort_keys=True))

    def hw_write(self, offset, size, value):
        result = super().hw_write(offset, size, value)
        if offset in self.HYPOTHESIS_OFFSETS:
            self.hypothesis_writes = min(self.hypothesis_writes + 1, 2**64 - 1)
            self._trace("write", offset, value)
        return result

    def hw_read(self, offset, size):
        value = super().hw_read(offset, size)
        if offset in self.HYPOTHESIS_OFFSETS:
            self.hypothesis_reads = min(self.hypothesis_reads + 1, 2**64 - 1)
            self._trace("read", offset, value)
        return value

    def control_observation(self):
        return dict(super().control_observation(), **self.analysis_facts(),
                    hypothesis_reads=self.hypothesis_reads,
                    hypothesis_writes=self.hypothesis_writes,
                    hypothesis_trace=[dict(event) for event in self.hypothesis_trace])
