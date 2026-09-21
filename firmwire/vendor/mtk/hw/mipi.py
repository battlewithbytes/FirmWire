"""Explicit MIPI initialization write-capture hypothesis, not serial hardware."""
import json

from firmwire.hw.configuration import ConfigurationRegisters
from firmwire.hw.peripheral import FirmWirePeripheral


class MTKMipiInitCapturePeripheral(FirmWirePeripheral):
    """Capture a reviewed bank layout without inventing readback or completion.

    Both targets share the initializer, but register fields and effects remain
    unresolved. Swallowing these writes is an explicit analysis assumption.
    The bank layout is configurable; no firmware PC, hash or value is special.
    The legacy lower BSI3 mapping is separate and is not validated by this model.
    """
    BANK_OFFSETS = (0, 0x1000, 0x2000, 0x3000, 0x4000)
    WORD_OFFSETS = (4, 0xc, 0x10, 0x44, 0x48, 0x4c, 0x50, 0x90)

    @classmethod
    def analysis_facts(cls):
        return dict(kind="mt6768-mipi-init-capture-analysis/v1", analysis_only=True,
                    hypothesis="capture-initialization-writes-without-effects/v1",
                    semantics_verified=False, boot_verified=False,
                    readback_supported=False, serial_transactions_supported=False,
                    completion_fabricated=False, guest_irq_routed=False,
                    assumptions=["reviewed initialization writes are consumed without effects",
                                 "captured words are diagnostic state, not hardware readback",
                                 "all reads and unreviewed writes remain unsupported"])

    def __init__(self, name, address, size, bank_offsets=BANK_OFFSETS, **kwargs):
        banks = tuple(bank_offsets)
        if (type(size) is not int or not 1 <= len(banks) <= 16 or
                len(set(banks)) != len(banks) or
                any(type(b) is not int or b < 0 or b % 4 or b + 0x94 > size for b in banks) or
                any(abs(a-b) < 0x94 for i, a in enumerate(banks) for b in banks[i+1:])):
            raise ValueError("invalid or overlapping MIPI capture banks")
        super().__init__(name, address, size, **kwargs)
        self.bank_offsets = banks
        self.config = ConfigurationRegisters(b+o for b in banks for o in self.WORD_OFFSETS)
        self.recent_accesses = []
        self.last_unsupported = None
        self.log.warning("MIPI PROVISIONAL WRITE CAPTURE %s", json.dumps(self.analysis_facts(), sort_keys=True))

    def enable_control_observer(self):
        pass  # Bounded observations are always available; no change to behavior.

    def _stop(self, direction, offset, size, reason):
        self.last_unsupported = dict(direction=direction, offset=offset, size=size, reason=reason)
        self.log.error("MIPI capture unsupported metadata=%s", json.dumps(self.control_observation(), sort_keys=True))

    def hw_read(self, offset, size):
        self._stop("read", offset, size, "read semantics unresolved")
        raise NotImplementedError("MIPI capture does not model reads")

    def hw_write(self, offset, size, value):
        try:
            result = self.config.write(offset, size, value)
        except (ValueError, NotImplementedError) as error:
            self._stop("write", offset, size, str(error))
            raise
        self.recent_accesses.append(dict(offset=offset, size=size, value=value))
        del self.recent_accesses[:-64]
        if self.config.writes <= 64:
            self.log.warning("MIPI capture access metadata=%s", json.dumps(self.control_observation(), sort_keys=True))
        return result

    def control_observation(self):
        return dict(self.config.facts(), **self.analysis_facts(), bank_offsets=list(self.bank_offsets),
                    recent_accesses=[dict(event) for event in self.recent_accesses],
                    capture_stop=None if self.last_unsupported is None else dict(self.last_unsupported))
