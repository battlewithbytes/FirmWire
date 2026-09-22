"""Selected dynamic level inputs on a normal MDCIRQ bank, explicitly opt-in.

Nonselected inputs are NOT emulated. Outside the normal bank, the caller keeps
its existing passthrough behavior. Mode registers are inspected to refuse
unsupported delivery, not advertised as complete edge/NMI/GCR emulation.
"""
from .mdcirq import MdcirqNormalIRQBank


class MdcirqLevelDelivery:
    def __init__(self, outputs, sources, *, minimum_inclusive, source_count=256):
        outputs = tuple(outputs)
        sources = tuple(sources)
        if (not sources or len(set(sources)) != len(sources)
                or any(type(s) is not int or not 0 <= s < source_count for s in sources)):
            raise ValueError("requires explicit unique selected sources")
        self.sources = sources
        self.bank = MdcirqNormalIRQBank(outputs, minimum_inclusive=minimum_inclusive,
                                        source_count=source_count)
        self.gcr_disabled = False
        self.sensitivity = [0] * ((source_count + 31)//32)
        self.broadcast = [0] * len(self.sensitivity)
        self.nmi = [0] * len(self.sensitivity)
        self.claims = [0] * len(outputs)
        self.returns = [0] * len(self.claims)
        self.last_claim = [None] * len(self.claims)
        self.input_assertions = {source: 0 for source in sources}
        self.raw_config = {}

    def _normal(self, offset, size):
        for name, base in self.bank.layout.items():
            if base <= offset < base + self.bank.widths[name]:
                self.bank._decode(offset, size)  # Enforce width/alignment before side effects.
                return name, (offset-base)//4
        return None, None

    def _validate_mode(self, source):
        bit, word = 1 << (source % 32), source // 32
        if not self.gcr_disabled:
            raise NotImplementedError("normal IRQ delivery requires GCR disabled by guest")
        if any(words[word] & bit for words in (self.sensitivity, self.broadcast, self.nmi)):
            raise NotImplementedError("selected source requires unsupported edge/broadcast/NMI delivery")

    def set_level(self, source, level):
        if type(source) is not int or source not in self.sources or type(level) is not bool:
            raise ValueError("unselected IRQ input or invalid level")
        if level:
            self._validate_mode(source)
            if not self.bank.core.levels[source]: self.input_assertions[source] += 1
        self.bank.set_level(source, level)

    def read(self, offset, size):
        name, index = self._normal(offset, size)
        if name is None: return None
        if (name, index) in self.raw_config: return self.raw_config[name, index]
        before = len(self.bank.core.active[index]) if name == "irq_id" else 0
        value = self.bank.read(offset, size)
        if name == "irq_id" and len(self.bank.core.active[index]) > before:
            self.claims[index] += 1
            self.last_claim[index] = value
        return value

    def write(self, offset, size, value):
        name, index = self._normal(offset, size)
        if name is not None:
            if name in ("priority", "group"):
                if type(value) is not int or not 0 <= value < 2**32:
                    raise ValueError("configuration word must fit 32 bits")
                # Shared words can include NMI-only group encodings belonging
                # to unselected sources. Preserve those original bytes for
                # readback; never route them through the normal-IRQ core.
                modeled = 0
                for byte in range(4):
                    source = index*4+byte
                    field = (value >> (8*byte)) & 255
                    if source not in self.sources: field = 127 if name == "priority" else 0
                    modeled |= field << (8*byte)
                self.bank.write(offset, modeled, size)
                self.raw_config[name, index] = value
            else:
                self.bank.write(offset, value, size)
            if name == "irq_return": self.returns[index] += 1
            return True
        # Track mode bits, while preserving the original caller's storage for
        # these and all unmodeled peripheral controls. No silent mode fallback.
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("register value must fit 32 bits")
        if offset == 0x1a8:
            if size != 4 or value != 1:
                raise NotImplementedError("GCR delivery is not implemented")
            self.gcr_disabled = True
        for base, values, op in ((0xa0,self.sensitivity,"set"), (0xc0,self.broadcast,"set"),
                (0x100,self.nmi,"set"), (0x160,self.sensitivity,"or"), (0x180,self.sensitivity,"clear")):
            if base <= offset < base+4*len(values):
                if size != 4 or offset % 4: raise ValueError("mode register requires aligned word")
                word = (offset-base)//4
                updated = value if op == "set" else (values[word] | value if op == "or" else values[word] & ~value)
                if any(s//32 == word and updated & (1 << (s%32)) for s in self.sources):
                    raise NotImplementedError("selected source requires unsupported IRQ mode")
                values[word] = updated
        # Do not silently accept a software-trigger request for a selected
        # external level input. Other inputs remain explicitly unmodeled.
        for base in (0x80, 0x120):
            if base <= offset < base+4*len(self.sensitivity):
                if size != 4 or offset % 4: raise ValueError("software trigger requires aligned word")
                word = (offset-base)//4
                if any(s//32 == word and value & (1 << (s%32)) for s in self.sources):
                    raise NotImplementedError("selected source software triggering is not implemented")
        return False

    def snapshot(self):
        core = self.bank.core
        return dict(schema="firmwire.mdcirq-level-delivery/v1", analysis_only=True,
            hardware_semantics_verified=False, gcr_disabled=self.gcr_disabled,
            unselected_inputs="unmodeled", other_registers="existing-passthrough",
            nonselected_config="stored-original-bytes-not-interpreted",
            minimum_inclusive=self.bank.minimum_inclusive,
            claims=list(self.claims), returns=list(self.returns), last_claim=list(self.last_claim),
            sources=[dict(source=s, asserted=core.levels[s], masked=core.masked[s],
                priority=self.bank.priorities[s], group=self.bank.groups[s],
                route_mask=self.bank.routes[self.bank.groups[s]],
                input_assertions=self.input_assertions[s]) for s in self.sources],
            minimum=list(self.bank.minimum), state=list(self.bank.state),
            controller=core.snapshot())
