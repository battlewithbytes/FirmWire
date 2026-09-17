"""Cold-start synchronization of the existing GCR analysis clock and HWPOR.

The source remains the legacy read-driven GCR counter, NOT silicon time. BSI
access synchronizes already-due events but does not advance time or retry reads.
The source interface can later accept a validated free-running virtual clock.
"""
from threading import RLock


class RfSequencerClock:
    HWPOR_TICKS_PER_GCR_UNIT = 75

    def __init__(self, counter):
        if not callable(counter):
            raise ValueError("Requires a monotonic analysis counter")
        self.counter = counter
        self.last = 0
        self.sinks = []
        self.lock = RLock()

    def attach(self, sequencer):
        if self.counter() != 0 or sequencer.ticks != 0:
            raise ValueError("RF clock attachment requires cold-start state")
        if sequencer in self.sinks:
            raise ValueError("Sequencer already attached")
        self.sinks.append(sequencer)

    def synchronize(self):
        with self.lock:
            now = self.counter()
            if type(now) is not int or now < self.last:
                raise ValueError("Guest clock must be monotonic; snapshot restore unsupported")
            ticks = (now - self.last) * self.HWPOR_TICKS_PER_GCR_UNIT
            # Flush already-due events before publishing the corresponding GCR
            # value or accepting another serial command. Same-time ordering is
            # scheduled events first, MMIO second; zero-time events are included.
            for sink in self.sinks:
                sink.advance(ticks)
            self.last = now
            return now

    def facts(self):
        return {"kind": "mt6768-gcr-hwpor-analysis/v1", "analysis_only": True,
                "source": "legacy-gcr-read-driven-counter",
                "hwpor_ticks_per_gcr_unit": self.HWPOR_TICKS_PER_GCR_UNIT,
                "dispatch": "due-events-before-gcr-or-bsi-mmio",
                "gcr_reads_advance_time": True, "bsi_polling_advances_time": False,
                "silicon_timing_verified": False,
                "snapshot_support": False}


def bind_rf_clock(peripherals):
    """Only software-RF sequencers opt in; legacy/non-RF boots are unchanged."""
    devices = [p for p in peripherals.values() if getattr(p, "hwpor", None) is not None]
    if not devices:
        return None
    gcr = peripherals.get("GCRCustom")
    if gcr is None or not hasattr(gcr, "bind_clock"):
        raise ValueError("Software RF requires the shared GCR clock adapter")
    clock = RfSequencerClock(lambda:gcr.timer)
    for device in devices:
        device.bind_clock(clock)
    gcr.bind_clock(clock)
    return clock
