"""Provisional BSI scheduler write capture; no guessed enable update policy."""
import json

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.peripheral import FirmWirePeripheral


class MTKBsiSchedulerCapturePeripheral(FirmWirePeripheral):
    WORD_OFFSETS = tuple(range(0, 0x20, 4))

    @classmethod
    def analysis_facts(cls):
        return dict(kind="mt6768-bsi-scheduler-enable-capture-analysis/v1", analysis_only=True,
                    hypothesis="capture-eight-enable-word-writes-without-effects/v1",
                    semantics_verified=False, boot_verified=False,
                    reset_known=False, readback_supported=False,
                    enable_update_policy=None,
                    event_identity_mapping_verified=False, event_dispatch_supported=False,
                    completion_fabricated=False, guest_irq_routed=False,
                    assumptions=["initialization writes consumed without hardware effects",
                                 "last written words are diagnostic capture, not enable state",
                                 "replacement, write-one-set, clear and pulse semantics unresolved",
                                 "no inferred reserved bits or event count from initializer values",
                                 "all reads, other command and descriptor accesses unsupported"])

    def __init__(self, name, address, size, word_offsets=WORD_OFFSETS, **kwargs):
        offsets = tuple(word_offsets)
        if (type(size) is not int or not 1 <= len(offsets) <= 64 or
                any(type(o) is not int or o < 0 or o % 4 or o+4 > size for o in offsets) or
                len(set(offsets)) != len(offsets)):
            raise ValueError("invalid BSI scheduler enable layout")
        super().__init__(name, address, size, **kwargs)
        self.word_offsets = offsets
        self.config = ConfigurationRegisters(offsets)
        self.recent_writes = []
        self.last_unsupported = None
        self.log.warning("BSI SCHEDULER PROVISIONAL WRITE CAPTURE %s", json.dumps(self.analysis_facts(), sort_keys=True))

    def enable_control_observer(self):
        pass

    def _stop(self, offset, size, direction, reason):
        self.last_unsupported = dict(offset=offset, size=size, direction=direction, reason=reason)
        self.log.error("BSI scheduler unsupported metadata=%s", json.dumps(self.control_observation(), sort_keys=True))

    def hw_read(self, offset, size):
        self._stop(offset, size, "read", "read semantics unresolved")
        raise NotImplementedError("BSI scheduler readback unsupported")

    def hw_write(self, offset, size, value):
        if (type(offset) is not int or offset < 0 or offset % 4 or type(size) is not int or size != 4 or
                type(value) is not int or not 0 <= value < 2**32):
            self._stop(offset, size, "write", "requires aligned unsigned 32-bit write")
            raise ValueError("invalid BSI scheduler enable write")
        if offset not in self.word_offsets:
            self._stop(offset, size, "write", "unreviewed register or command")
            raise NotImplementedError("BSI scheduler command/descriptor unsupported at %#x" % offset)
        bank = self.word_offsets.index(offset)
        self.config.write(offset, size, value)
        self.recent_writes.append(dict(offset=offset, bank=bank, value=value))
        del self.recent_writes[:-64]
        if self.config.writes <= 64:
            self.log.warning("BSI scheduler capture metadata=%s", json.dumps(self.control_observation(), sort_keys=True))
        return True

    def control_observation(self):
        return dict(self.analysis_facts(), captured_words=self.config.facts(), word_offsets=list(self.word_offsets),
                    recent_writes=[dict(w) for w in self.recent_writes],
                    last_unsupported=None if self.last_unsupported is None else dict(self.last_unsupported))
