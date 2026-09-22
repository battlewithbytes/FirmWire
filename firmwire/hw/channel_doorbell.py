"""Reusable pending-channel bits, independent of payloads and CPU IRQ wiring."""


class ChannelDoorbell:
    def __init__(self, channels, on_change=None):
        if type(channels) is not int or not 1 <= channels <= 32:
            raise ValueError("doorbell channel count must be 1..32")
        if on_change is not None and not callable(on_change):
            raise ValueError("doorbell callback must be callable")
        self.channels = channels
        self.on_change = on_change
        self.pending = 0
        self.notifications = [0] * channels
        self.coalesced = [0] * channels
        self.acknowledged = [0] * channels

    def _update(self, pending):
        previous = self.pending
        self.pending = pending
        if pending != previous and self.on_change is not None:
            self.on_change(pending)

    def notify(self, channel):
        if type(channel) is not int or not 0 <= channel < self.channels:
            raise ValueError("invalid doorbell channel")
        self.notifications[channel] += 1
        bit = 1 << channel
        if self.pending & bit:
            self.coalesced[channel] += 1
        self._update(self.pending | bit)

    def acknowledge(self, mask):
        if type(mask) is not int or not 0 <= mask < 2**32:
            raise ValueError("acknowledgement mask must fit 32 bits")
        cleared = self.pending & mask
        for channel in range(self.channels):
            if cleared & (1 << channel):
                self.acknowledged[channel] += 1
        self._update(self.pending & ~mask)
        return cleared

    def snapshot(self):
        return dict(schema="firmwire.channel-doorbell/v1", channels=self.channels,
                    pending=self.pending, notifications=list(self.notifications),
                    coalesced=list(self.coalesced), acknowledged=list(self.acknowledged))
