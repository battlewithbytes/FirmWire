"""Translate disjoint channel bit masks into level inputs; no vendor/CPU ABI."""


class ChannelIRQRouter:
    def __init__(self, routes):
        self.routes = tuple(routes)
        if not self.routes or len(self.routes) > 32:
            raise ValueError("requires 1..32 channel routes")
        seen = 0
        for mask, sink in self.routes:
            if type(mask) is not int or not 0 < mask < 2**32 or mask & seen or not callable(sink):
                raise ValueError("channel masks must be nonzero, disjoint and have a sink")
            seen |= mask
        self.levels = [False] * len(self.routes)
        self.transitions = [0] * len(self.routes)

    def __call__(self, pending):
        if type(pending) is not int or not 0 <= pending < 2**32:
            raise ValueError("pending channels must fit 32 bits")
        for index, (mask, sink) in enumerate(self.routes):
            level = bool(pending & mask)
            if self.levels[index] != level:
                sink(level)
                self.levels[index] = level
                self.transitions[index] += 1

    def snapshot(self):
        return dict(masks=[mask for mask, _ in self.routes], levels=list(self.levels),
                    transitions=list(self.transitions))
