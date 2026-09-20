"""93xx LTE timer RR-value storage, not a running timer or interrupt source.

Set/Dump RR Value access two four-word banks with stride 0x14. Trigger RR
Event uses the preceding words (+0x58/+0x6c), intentionally NOT accepted.
See docs/lte-timer.md for exact-ROM and sibling-symbol evidence.
"""
import json

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.event_queue import MaskedEventQueue
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


class MTKLTETimerGroupCancelAnalysisPeripheral(MTKLTETimerInitStorageAnalysisPeripheral):
    """Cancellation-only command frontend; no guest trigger/clock/IRQ claim.

    Each command bit cancels the corresponding queued event in that group.
    A zero write cancels nothing; it is not stored as a configuration value.
    Pending/delivered IRQ acknowledgments and +0x408/+0x40c remain unsupported.
    Empty power-on event state and the inherited init words are analysis
    assumptions. Guest scheduling remains strict until its format is reviewed.
    """
    GROUP_CANCEL_OFFSETS = tuple(0x1ba0 + group * 12 for group in range(5))
    KIND = "93xx-lte-group-cancel-analysis/v1"

    @classmethod
    def analysis_facts(cls):
        facts = super().analysis_facts()
        facts.update(configuration_only=False, group_cancel_supported=True, guest_trigger_supported=False,
                     command_semantics_verified=False, empty_initial_events_assumed=True,
                     cancellation_model="selected-queued-events-on-write/v1",
                     unresolved_command_facts=["strobe edge/level behavior", "reserved event bits",
                                               "pending IRQ acknowledgment", "clock and IRQ mapping"])
        return facts

    def __init__(self, name, address, size, **kwargs):
        if size < max(self.GROUP_CANCEL_OFFSETS) + 4:
            raise ValueError("LTE group command window is too small")
        super().__init__(name, address, size, **kwargs)
        self.group_events = MaskedEventQueue(len(self.GROUP_CANCEL_OFFSETS), width=32)
        self.cancel_trace = []

    def hw_write(self, offset, size, value):
        if type(offset) is int and offset in self.GROUP_CANCEL_OFFSETS:
            bank = self.GROUP_CANCEL_OFFSETS.index(offset)
            return self._cancel_write(self.group_events, self.cancel_trace,
                                      "LTE group cancel metadata=%s", bank, offset, size, value)
        return super().hw_write(offset, size, value)

    def _cancel_write(self, queue, trace, label, bank, offset, size, value):
        if type(size) is not int or size != 4 or type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("cancel command requires an aligned unsigned 32-bit write")
        removed = queue.cancel(bank, value)
        trace.append(dict(offset=offset, mask=value, cancelled_mask=removed))
        del trace[:-32]
        if queue.cancel_writes <= 32:
            self.log.warning(label, json.dumps(self.control_observation(), sort_keys=True))
        return True

    def control_observation(self):
        return dict(super().control_observation(), group_events=self.group_events.facts(),
                    cancel_trace=[dict(event) for event in self.cancel_trace])


class MTKLTETimerEventCancelAnalysisPeripheral(MTKLTETimerGroupCancelAnalysisPeripheral):
    """Provisional per-event cancellation, distinct from group cancellation.

    Reviewed target disable bodies strobe +0x408/+0x40c before separately
    canceling group 4. No group-to-event identity mapping is inferred here.
    Guest scheduling and timer back-door programming/readback remain strict;
    the empty initial queue and cancel-on-write interpretation are assumptions.
    """
    EVENT_CANCEL_OFFSETS = (0x408, 0x40c)
    KIND = "93xx-lte-event-cancel-analysis/v1"

    @classmethod
    def analysis_facts(cls):
        facts = super().analysis_facts()
        facts.update(event_cancel_supported=True, group_to_event_mapping_verified=False,
                     timer_readback_supported=False,
                     event_cancellation_model="independent-selected-queued-events-on-write/v1")
        facts["unresolved_command_facts"] += ["group-to-event identity mapping",
                                              "timer programming/readback effects"]
        return facts

    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, **kwargs)
        # Bits are command identities, not fabricated timer addresses or IRQs.
        self.individual_events = MaskedEventQueue(len(self.EVENT_CANCEL_OFFSETS), width=32)
        self.event_cancel_trace = []

    def hw_write(self, offset, size, value):
        if type(offset) is int and offset in self.EVENT_CANCEL_OFFSETS:
            bank = self.EVENT_CANCEL_OFFSETS.index(offset)
            return self._cancel_write(self.individual_events, self.event_cancel_trace,
                                      "LTE event cancel metadata=%s", bank, offset, size, value)
        return super().hw_write(offset, size, value)

    def control_observation(self):
        return dict(super().control_observation(), individual_events=self.individual_events.facts(),
                    event_cancel_trace=[dict(event) for event in self.event_cancel_trace])
