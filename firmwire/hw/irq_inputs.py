"""Register/CPU-independent external levels OR latched software requests.

Single emulator thread only. A software clear never lowers an external input.
Updates are validated completely before publishing one atomic batch.
"""


class IRQInputLatch:
    def __init__(self, source_count, publish):
        if type(source_count) is not int or not 1 <= source_count <= 4096 or not callable(publish):
            raise ValueError("invalid input latch geometry or callback")
        self.external = [False] * source_count
        self.software = [False] * source_count
        self.publish = publish

    def update(self, kind, updates):
        if kind not in ("external", "software"):
            raise ValueError("unknown input owner")
        checked, seen = [], set()
        for source, level in updates:
            if (type(source) is not int or not 0 <= source < len(self.external)
                    or source in seen or type(level) is not bool):
                raise ValueError("invalid or duplicate input update")
            checked.append((source, level))
            seen.add(source)
        selected = getattr(self, kind)
        other = self.software if kind == "external" else self.external
        # The sink validates its own geometry; it must not re-enter this latch.
        self.publish([(source, level or other[source]) for source, level in checked])
        for source, level in checked:
            selected[source] = level
