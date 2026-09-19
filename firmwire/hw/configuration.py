"""Strict storage for reviewed, passive 32-bit configuration registers.

Not a register-file fallback: commands/status must be handled by the device.
Reset values are unknown until written, rather than assumed to be zero.
"""


class ConfigurationRegisters:
    def __init__(self, offsets):
        offsets = tuple(offsets)
        if (not 1 <= len(offsets) <= 1024 or
                any(type(x) is not int or x < 0 or x >= 2**32 or x % 4 for x in offsets) or
                len(set(offsets)) != len(offsets)):
            raise ValueError("configuration requires unique aligned word offsets")
        self.offsets = tuple(sorted(offsets))
        self.reset()

    def reset(self):
        self._values = {}
        self.reads = self.writes = 0
        self.last_unsupported = None

    def _check(self, offset, size, direction):
        if (type(offset) is not int or type(size) is not int or
                offset < 0 or offset >= 2**32 or offset % 4 or size != 4):
            raise ValueError("configuration requires aligned 32-bit access")
        if offset not in self.offsets:
            self.last_unsupported = dict(offset=offset, size=size, direction=direction,
                                         reason="unreviewed register")
            raise NotImplementedError("unreviewed configuration %s at %#x" % (direction, offset))

    def read(self, offset, size):
        self._check(offset, size, "read")
        if offset not in self._values:
            self.last_unsupported = dict(offset=offset, size=size, direction="read",
                                         reason="reset value unknown")
            raise NotImplementedError("configuration reset value unknown at %#x" % offset)
        self.reads = min(self.reads + 1, 2**64 - 1)
        return self._values[offset]

    def write(self, offset, size, value):
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("configuration value must be an unsigned word")
        self._check(offset, size, "write")
        self._values[offset] = value
        self.writes = min(self.writes + 1, 2**64 - 1)
        return True

    def facts(self):
        return dict(registers=[dict(offset=x, value=self._values.get(x)) for x in self.offsets],
                    reads=self.reads, writes=self.writes,
                    last_unsupported=None if self.last_unsupported is None else dict(self.last_unsupported))
