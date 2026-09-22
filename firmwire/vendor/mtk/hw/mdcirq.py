"""Normal-IRQ register adapter experiment; NOT automatically installed.

Offsets describe the reviewed legacy normal page, not a physical mapping.
Only this explicit subset is implemented. Unsupported registers fail instead
of quietly pretending to implement NMI, edge/broadcast, GCR or software IRQs.
Minimum-priority equality is an explicit analysis policy pending silicon proof.
"""
from firmwire.hw.routed_irq import RoutedLevelIRQController


NORMAL_IRQ_LAYOUT = dict(mask=0x20, mask_clear=0x40, mask_set=0x60,
    current_id=0x200, current_priority=0x220, minimum=0x260, priority=0x300,
    state=0x400, group=0x500, route=0x600, irq_id=0xc20,
    priority_ack=0xc60, irq_return=0xc70)


class MdcirqNormalIRQBank:
    def __init__(self, outputs, *, minimum_inclusive, source_count=256, layout=None):
        outputs = tuple(outputs)
        if type(minimum_inclusive) is not bool:
            raise ValueError("choose an explicit minimum-priority equality policy")
        if type(source_count) is not int or not 1 <= source_count <= 256:
            raise ValueError("normal IRQ IDs must fit eight bits")
        if not 1 <= len(outputs) <= 32 or not all(callable(sink) for sink in outputs):
            raise ValueError("invalid IRQ outputs")
        self.layout = dict(NORMAL_IRQ_LAYOUT if layout is None else layout)
        if set(self.layout) != set(NORMAL_IRQ_LAYOUT):
            raise ValueError("register layout must define the complete supported subset")
        self.sources, self.vpes = source_count, len(outputs)
        vector_size = ((source_count + 31) // 32) * 4
        packed_size = ((source_count + 3) // 4) * 4
        self.widths = dict.fromkeys(self.layout, self.vpes * 4)
        self.widths.update(mask=vector_size, mask_clear=vector_size, mask_set=vector_size,
                           priority=packed_size, group=packed_size, route=16 * 4)
        intervals = []
        for name, offset in self.layout.items():
            if type(offset) is not int or offset < 0 or offset % 4 or offset + self.widths[name] > 0x10000:
                raise ValueError("invalid register geometry")
            intervals.append((offset, offset + self.widths[name]))
        intervals.sort()
        if any(end > start for (_, end), (start, _) in zip(intervals, intervals[1:])):
            raise ValueError("overlapping register banks for this output count")
        self.minimum_inclusive = minimum_inclusive
        self.outputs = outputs
        self.desired = [False] * self.vpes
        self.driven = [False] * self.vpes
        self.core = RoutedLevelIRQController(source_count,
            [lambda level, index=index: self.desired.__setitem__(index, level)
             for index in range(self.vpes)])
        self.priorities = [127] * source_count
        self.groups = [0] * source_count
        self.routes = [(1 << self.vpes) - 1] * 16
        self.minimum = [127] * self.vpes
        self.state = [511] * self.vpes
        for vpe in range(self.vpes):
            self.core.set_threshold(vpe, 127 + int(minimum_inclusive))

    def _decode(self, offset, size):
        if type(offset) is not int or type(size) is not int or size != 4 or offset % 4:
            raise ValueError("normal IRQ adapter requires aligned 32-bit accesses")
        for name, base in self.layout.items():
            if base <= offset < base + self.widths[name]:
                return name, (offset - base) // 4
        raise NotImplementedError("unimplemented MDCIRQ register offset %#x" % offset)

    def _flush(self):
        for index, level in enumerate(self.desired):
            if level != self.driven[index]:
                self.outputs[index](level)
                self.driven[index] = level

    def _sync_routes(self):
        # Internal callbacks accumulate final levels. A packed register update
        # never publishes intermediate per-source route changes to the CPU.
        for source in range(self.sources):
            mask = self.routes[self.groups[source]]
            self.core.configure(source, priority=self.priorities[source],
                targets=[vpe for vpe in range(self.vpes) if not mask & (1 << vpe)])

    def set_level(self, source, level):
        self.core.set_level(source, level)
        self._flush()

    def read(self, offset, size=4):
        name, index = self._decode(offset, size)
        if name == "mask":
            return sum(int(masked) << bit for bit, masked in
                       enumerate(self.core.masked[index*32:(index+1)*32]))
        if name in ("priority", "group"):
            values = self.priorities if name == "priority" else self.groups
            return sum(value << (8*byte) for byte, value in enumerate(values[index*4:(index+1)*4]))
        if name in ("route", "minimum", "state"):
            values = self.routes if name == "route" else getattr(self, name)
            return values[index]
        if name == "irq_id":
            source = self.core.claim(index)
            self._flush()
            if source is not None:
                return source
            return self.core.active[index][-1][0] if self.core.active[index] else 0x1ff
        if name == "current_id":
            return self.core.active[index][-1][0] if self.core.active[index] else 0x1ff
        if name == "current_priority":
            return self.core.active[index][-1][1] if self.core.active[index] else 127
        raise NotImplementedError("unimplemented MDCIRQ read: " + name)

    def write(self, offset, value, size=4):
        name, index = self._decode(offset, size)
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("register value must fit 32 bits")
        if name in ("mask", "mask_clear", "mask_set"):
            for source in range(index*32, min((index+1)*32, self.sources)):
                bit = bool(value & (1 << (source % 32)))
                if name == "mask" or bit:
                    self.core.set_mask(source, bit if name == "mask" else name == "mask_set")
        elif name in ("priority", "group"):
            values = self.priorities if name == "priority" else self.groups
            decoded = [(value >> (8*byte)) & 255 for byte in range(min(4, self.sources-index*4))]
            if any(byte >= (128 if name == "priority" else 16) for byte in decoded):
                raise NotImplementedError("unsupported priority/group encoding")
            values[index*4:index*4+len(decoded)] = decoded
            self._sync_routes()
        elif name == "route":
            if value >> self.vpes:
                raise ValueError("route mask exceeds configured outputs")
            self.routes[index] = value
            self._sync_routes()
        elif name in ("minimum", "state"):
            if value > (127 if name == "minimum" else 511):
                raise ValueError("invalid priority/state value")
            getattr(self, name)[index] = value
            limit = self.minimum[index] + int(self.minimum_inclusive)
            self.core.set_threshold(index, min(limit, self.state[index], 128))
        elif name == "irq_return":
            stack = self.core.active[index]
            expected = stack[-2][0] if len(stack) > 1 else 0x1ff
            if not stack or value & 0x1ff != expected:
                raise ValueError("IRQ return must restore the previous ID")
            self.core.complete(index, stack[-1][0])
        elif name == "priority_ack":
            if value != 0x1ff or self.core.active[index]:
                raise NotImplementedError("only idle priority-stack initialization is modeled")
        else:
            raise NotImplementedError("unimplemented MDCIRQ write: " + name)
        self._flush()

    def snapshot(self):
        return dict(schema="firmwire.mdcirq-normal-bank-analysis/v1", analysis_only=True,
                    hardware_semantics_verified=False, minimum_inclusive=self.minimum_inclusive,
                    layout=dict(self.layout), controller=self.core.snapshot(),
                    unsupported=["NMI", "edge", "broadcast", "GCR", "software triggers",
                                 "automatic CPU ACK", "active priority-ACK writes"])
