"""Selected dynamic level inputs on a normal MDCIRQ bank, explicitly opt-in.

Nonselected inputs are NOT emulated. Outside the normal bank, the caller keeps
its existing passthrough behavior. Mode registers are inspected to refuse
unsupported delivery, not advertised as complete edge/NMI/GCR emulation.
"""
from collections import deque
from firmwire.hw.irq_inputs import IRQInputLatch
from .mdcirq import MdcirqNormalIRQBank


class MdcirqLevelDelivery:
    def __init__(self, outputs, sources, *, minimum_inclusive, source_count=256, software_sources=()):
        outputs = tuple(outputs)
        sources = tuple(sources)
        if (not sources or len(set(sources)) != len(sources)
                or any(type(s) is not int or not 0 <= s < source_count for s in sources)):
            raise ValueError("requires explicit unique selected sources")
        software_sources = tuple(software_sources)
        if (len(set(software_sources)) != len(software_sources)
                or any(type(s) is not int or not 0 <= s < source_count for s in software_sources)):
            raise ValueError("invalid selected software sources")
        self.external_sources = sources
        self.software_sources = software_sources
        self.sources = tuple(sorted(set(sources) | set(software_sources)))
        self.bank = MdcirqNormalIRQBank(outputs, minimum_inclusive=minimum_inclusive,
                                        source_count=source_count)
        self.gcr_disabled = False
        self.inputs = IRQInputLatch(source_count, self.bank.set_levels)
        self.software_level = set()  # Require explicit guest level configuration.
        self.software_events = deque(maxlen=32)
        self.software_writes = 0
        self.software_posts = {s: 0 for s in software_sources}
        self.software_clears = {s: 0 for s in software_sources}
        self.sensitivity = [0] * ((source_count + 31)//32)
        self.broadcast = [0] * len(self.sensitivity)
        self.nmi = [0] * len(self.sensitivity)
        self.claims = [0] * len(outputs)
        self.returns = [0] * len(self.claims)
        self.last_claim = [None] * len(self.claims)
        self.source_claims = {s: 0 for s in self.sources}
        self.source_returns = {s: 0 for s in self.sources}
        self.input_assertions = {source: 0 for source in self.sources}
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
        if source in self.software_sources:
            if source not in self.software_level or self.nmi[word] & bit:
                raise NotImplementedError("software source requires explicitly configured normal level mode")
            self._validate_software_route(source)
            return
        if any(words[word] & bit for words in (self.sensitivity, self.broadcast, self.nmi)):
            raise NotImplementedError("selected source requires unsupported edge/broadcast/NMI delivery")

    def _validate_software_route(self, source, group=None, route=None, broadcast=None):
        word, bit = source // 32, 1 << (source % 32)
        broadcast = self.broadcast[word] if broadcast is None else broadcast
        group = self.bank.groups[source] if group is None else group
        route = self.bank.routes[group] if route is None else route
        if broadcast & bit and ((~route) & ((1 << self.bank.vpes)-1)).bit_count() > 1:
            raise NotImplementedError("software broadcast to multiple outputs is not implemented")

    def set_level(self, source, level):
        if type(source) is not int or source not in self.external_sources or type(level) is not bool:
            raise ValueError("unselected IRQ input or invalid level")
        if level:
            self._validate_mode(source)
            if not self.inputs.external[source]: self.input_assertions[source] += 1
        self.inputs.update("external", [(source, level)])

    def merge_software_read(self, offset, size, value):
        """Replace selected direct-bank bits only; preserve unmodeled storage."""
        if 0x80 <= offset < 0x80 + 4*len(self.sensitivity) and self.software_sources:
            if size != 4 or offset % 4:
                raise ValueError("software register requires aligned word")
            word = (offset-0x80)//4
            for source in self.software_sources:
                if source//32 == word:
                    bit = 1 << (source%32)
                    value = (value & ~bit) | (bit if self.inputs.software[source] else 0)
        return value

    def read(self, offset, size):
        name, index = self._normal(offset, size)
        if name is None: return None
        if (name, index) in self.raw_config: return self.raw_config[name, index]
        before = len(self.bank.core.active[index]) if name == "irq_id" else 0
        value = self.bank.read(offset, size)
        if name == "irq_id" and len(self.bank.core.active[index]) > before:
            self.claims[index] += 1
            self.last_claim[index] = value
            self.source_claims[value] += 1
        return value

    def write(self, offset, size, value):
        name, index = self._normal(offset, size)
        if name is not None:
            returned_source = (self.bank.core.active[index][-1][0]
                if name == "irq_return" and self.bank.core.active[index] else None)
            if name in ("priority", "group"):
                if type(value) is not int or not 0 <= value < 2**32:
                    raise ValueError("configuration word must fit 32 bits")
                if name == "group":
                    for source in self.software_sources:
                        if source//4 == index and self.inputs.software[source]:
                            group = (value >> (8*(source%4))) & 255
                            if group >= 16: raise NotImplementedError("unsupported normal IRQ group")
                            self._validate_software_route(source, group=group)
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
                if name == "route":
                    for source in self.software_sources:
                        if self.bank.groups[source] == index and self.inputs.software[source]:
                            self._validate_software_route(source, route=value)
                self.bank.write(offset, value, size)
            if name == "irq_return":
                self.returns[index] += 1
                self.source_returns[returned_source] += 1
            return True
        # Track mode bits, while preserving the original caller's storage for
        # these and all unmodeled peripheral controls. No silent mode fallback.
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError("register value must fit 32 bits")
        if self.software_sources and 0x1b0 <= offset < 0x1bc:
            if size != 4 or offset % 4: raise ValueError("VPE mask requires aligned word")
            if value:
                raise NotImplementedError("VPE-wide IRQ/NMI masks require a separate reviewed model")
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
                if any(s//32 == word and updated & (1 << (s%32)) for s in self.external_sources
                       if s not in self.software_sources):
                    raise NotImplementedError("selected source requires unsupported IRQ mode")
                new_levels = set(self.software_level)
                for source in self.software_sources:
                    if source//32 != word: continue
                    bit = 1 << (source%32)
                    if base == 0xa0 or (base in (0x160, 0x180) and value & bit):
                        level = bool(value & bit) if base == 0xa0 else base == 0x180
                        if level: new_levels.add(source)
                        else: new_levels.discard(source)
                        if self.inputs.software[source] and not level:
                            raise NotImplementedError("pending software source cannot become edge triggered")
                    if self.inputs.software[source]:
                        if base == 0x100 and updated & bit:
                            raise NotImplementedError("pending software source cannot become NMI")
                        if base == 0xc0:
                            self._validate_software_route(source, broadcast=updated)
                values[word] = updated
                self.software_level = new_levels
        # Do not silently accept a software-trigger request for a selected
        # external level input. Other inputs remain explicitly unmodeled.
        # +0x120 is CLEAR, not SET: clearing software state cannot lower
        # an external input. See docs/mdcirq-software-trigger-banks.md.
        for base in (0x80, 0x120, 0x140):
            if base <= offset < base+4*len(self.sensitivity):
                if size != 4 or offset % 4: raise ValueError("software trigger requires aligned word")
                word = (offset-base)//4
                if base != 0x120 and any(s//32 == word and value & (1 << (s%32))
                        for s in self.external_sources if s not in self.software_sources):
                    raise NotImplementedError("selected source software triggering is not implemented")
                updates = [(s, bool(value & (1 << (s%32))) if base == 0x80 else base == 0x140)
                    for s in self.software_sources if s//32 == word
                    and (base == 0x80 or value & (1 << (s%32)))]
                for source, level in updates:
                    if level: self._validate_mode(source)
                if updates:
                    self.inputs.update("software", updates)
                    for source, level in updates:
                        counts = self.software_posts if level else self.software_clears
                        counts[source] += 1
                    self.software_writes += 1
                    self.software_events.append(dict(ordinal=self.software_writes, offset=offset,
                        value=value, updates=[dict(source=s,pending=p) for s,p in updates]))
        return False

    def snapshot(self):
        core = self.bank.core
        result = dict(schema="firmwire.mdcirq-level-delivery/v1", analysis_only=True,
            hardware_semantics_verified=False, gcr_disabled=self.gcr_disabled,
            unselected_inputs="unmodeled", other_registers="existing-passthrough",
            nonselected_config="stored-original-bytes-not-interpreted",
            minimum_inclusive=self.bank.minimum_inclusive,
            claims=list(self.claims), returns=list(self.returns), last_claim=list(self.last_claim),
            sources=[dict(source=s, asserted=core.levels[s], masked=core.masked[s],
                priority=self.bank.priorities[s], group=self.bank.groups[s],
                route_mask=self.bank.routes[self.bank.groups[s]],
                input_assertions=self.input_assertions[s], claims=self.source_claims[s],
                returns=self.source_returns[s]) for s in self.sources],
            minimum=list(self.bank.minimum), state=list(self.bank.state),
            controller=core.snapshot())
        if self.software_sources:
            result["software"] = dict(schema="firmwire.mdcirq-software-pending/v1",
                broadcast="zero-or-one-eligible-output-only", writes=self.software_writes,
                recent=list(self.software_events), sources=[dict(source=s,
                    pending=self.inputs.software[s], posts=self.software_posts[s],
                    clears=self.software_clears[s], level_configured=s in self.software_level)
                    for s in self.software_sources])
        return result
