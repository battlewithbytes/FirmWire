"""Reusable, single-threaded *analysis* controller for routed level interrupts.

This is not an MDCIRQ register ABI. A reviewed platform adapter must translate
mask polarity, source/group IDs, priority/state registers, claim and completion.
No image offsets, CPU count, register addresses or CPU IRQ pins live here.
Dynamic arbitration uses the lowest eligible output; this is a deterministic
analysis policy, not a claim about silicon arbitration. Broadcast, edge/NMI,
and timers are deliberately outside this primitive's contract.
"""


class RoutedLevelIRQController:
    def __init__(self, source_count, outputs, priority_levels=128):
        if type(source_count) is not int or not 1 <= source_count <= 4096:
            raise ValueError("invalid source count")
        if type(priority_levels) is not int or not 1 <= priority_levels <= 256:
            raise ValueError("invalid priority count")
        outputs = tuple(outputs)
        if not 1 <= len(outputs) <= 32 or not all(callable(output) for output in outputs):
            raise ValueError("requires 1..32 explicit output callbacks")
        self.source_count = source_count
        self.outputs = outputs
        self.priority_levels = priority_levels
        self.levels = [False] * source_count
        self.masked = [True] * source_count
        self.priority = [priority_levels - 1] * source_count
        self.routes = [frozenset() for _ in range(source_count)]
        self.threshold = [priority_levels] * len(outputs)
        self.active = [[] for _ in outputs]
        self.output_levels = [False] * len(outputs)

    def _source(self, source):
        if type(source) is not int or not 0 <= source < self.source_count:
            raise ValueError("invalid interrupt source")

    def _output(self, output):
        if type(output) is not int or not 0 <= output < len(self.outputs):
            raise ValueError("invalid interrupt output")

    def configure(self, source, *, priority, targets):
        self._source(source)
        if type(priority) is not int or not 0 <= priority < self.priority_levels:
            raise ValueError("invalid interrupt priority")
        targets = tuple(targets)
        for output in targets:
            self._output(output)
        if len(set(targets)) != len(targets):
            raise ValueError("duplicate interrupt output")
        self.priority[source] = priority
        self.routes[source] = frozenset(targets)
        self._update()

    def configure_many(self, updates):
        """Atomically update a packed bank without transient output edges."""
        checked = []
        seen = set()
        for source, priority, targets in updates:
            self._source(source)
            if source in seen or type(priority) is not int or not 0 <= priority < self.priority_levels:
                raise ValueError("invalid/duplicate batch source or priority")
            targets = tuple(targets)
            for output in targets: self._output(output)
            if len(set(targets)) != len(targets): raise ValueError("duplicate interrupt output")
            checked.append((source, priority, frozenset(targets)))
            seen.add(source)
        for source, priority, targets in checked:
            self.priority[source], self.routes[source] = priority, targets
        self._update()

    def set_level(self, source, level):
        self.set_levels([(source, level)])

    def set_levels(self, updates):
        checked, seen = [], set()
        for source, level in updates:
            self._source(source)
            if type(level) is not bool or source in seen:
                raise ValueError("invalid or duplicate interrupt level")
            checked.append((source, level))
            seen.add(source)
        for source, level in checked:
            self.levels[source] = level
        self._update()

    def set_mask(self, source, masked):
        self._source(source)
        if type(masked) is not bool:
            raise ValueError("interrupt mask must be boolean")
        self.masked[source] = masked
        self._update()

    def set_threshold(self, output, exclusive_priority):
        """Only numerically smaller priorities are deliverable."""
        self._output(output)
        if type(exclusive_priority) is not int or not 0 <= exclusive_priority <= self.priority_levels:
            raise ValueError("invalid interrupt threshold")
        self.threshold[output] = exclusive_priority
        self._update()

    def _pending(self):
        pending = [[] for _ in self.outputs]
        claimed = {source for stack in self.active for source, _ in stack}
        for source in range(self.source_count):
            if not self.levels[source] or self.masked[source] or source in claimed:
                continue
            priority = self.priority[source]
            for output in sorted(self.routes[source]):
                limit = self.threshold[output]
                if self.active[output]:
                    limit = min(limit, self.active[output][-1][1])
                if priority < limit:
                    pending[output].append((priority, source))
                    break
        return pending

    def _update(self):
        desired = [bool(items) for items in self._pending()]
        # Platform callbacks must run on the emulator thread and not re-enter.
        for output, level in enumerate(desired):
            if level != self.output_levels[output]:
                self.outputs[output](level)
                self.output_levels[output] = level

    def claim(self, output):
        self._output(output)
        pending = self._pending()[output]
        if not pending:
            return None  # Register adapter supplies its own spurious encoding.
        priority, source = min(pending)
        self.active[output].append((source, priority))
        self._update()
        return source

    def complete(self, output, source):
        self._output(output)
        self._source(source)
        if not self.active[output] or self.active[output][-1][0] != source:
            raise ValueError("completion must match the innermost claimed source")
        self.active[output].pop()
        self._update()

    def snapshot(self):
        return dict(schema="firmwire.routed-level-irq-analysis/v1", analysis_only=True,
                    hardware_semantics_verified=False, source_count=self.source_count,
                    output_count=len(self.outputs), arbitration="lowest-eligible-output",
                    pending=[[source for _, source in sorted(items)] for items in self._pending()],
                    active=[[source for source, _ in stack] for stack in self.active],
                    output_levels=list(self.output_levels))
