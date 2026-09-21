"""Digital immediate-serial control boundary; no RF or DSP results.

Observe mode preserves the old RAM behavior. Pending mode offers an empty
controller, accepts a command, then remains busy until an explicit backend
completion. It never treats an absent radio as a successful transaction.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SerialCommand:
    sequence: int
    bank: int
    port: int
    read: bool
    extended: bool
    data: tuple
    lengths: tuple


@dataclass(frozen=True)
class ReadCompletionLayout:
    status_offset: int
    clear_offset: int
    bank_ready_bits: tuple


class BsiImmediateControl:
    def __init__(self, size=0x9000, bank_offsets=(0x1000, 0x1100), mode="observe", read_layout=None,
                 serial_bus=None):
        if mode not in ("observe", "pending"):
            raise ValueError("Unknown BSI analysis mode")
        if type(size) is not int or not 0x20 <= size <= 0x100000:
            raise ValueError("Invalid BSI bank size")
        if (not 1 <= len(bank_offsets) <= 8 or len(set(bank_offsets)) != len(bank_offsets)
                or any(type(b) is not int or b < 0 or b % 4 or b + 0x20 > size for b in bank_offsets)
                or any(abs(a-b) < 0x20 for i, a in enumerate(bank_offsets) for b in bank_offsets[i+1:])):
            raise ValueError("Invalid or overlapping BSI banks")
        self.size, self.banks, self.mode = size, tuple(bank_offsets), mode
        if read_layout is not None:
            if not isinstance(read_layout, ReadCompletionLayout):
                raise ValueError("Expected an explicit BSI read-completion layout")
            offsets = (read_layout.status_offset, read_layout.clear_offset)
            bits = read_layout.bank_ready_bits
            if (len(bits) != len(self.banks) or len(set(bits)) != len(bits)
                    or any(type(bit) is not int or not 0 <= bit < 32 for bit in bits)
                    or offsets[0] == offsets[1]
                    or any(type(off) is not int or off < 0 or off % 4 or off + 4 > size
                           or any(b <= off < b + 0x20 for b in self.banks) for off in offsets)):
                raise ValueError("Invalid or overlapping BSI read-completion layout")
        self.read_layout = (ReadCompletionLayout(offsets[0], offsets[1], tuple(bits))
                            if read_layout is not None else None)
        if serial_bus is not None and mode != "pending":
            raise ValueError("Serial backend requires pending mode")
        self.serial_bus = serial_bus
        self.reset()

    def reset(self):
        self.storage = bytearray(self.size)
        self.pending = {}
        self.pending_blockers = {}
        self.sequence = self.completed = self.busy_rejections = 0
        self.completed_reads = self.read_status = 0
        self.events = []
        if self.serial_bus is not None:
            self.serial_bus.reset()

    def _access(self, offset, size):
        if (type(offset) is not int or type(size) is not int or size not in (1, 2, 4)
                or offset < 0 or offset % size or offset + size > self.size):
            raise ValueError("BSI requires aligned, in-bank 1/2/4-byte access")

    def _word(self, offset):
        return int.from_bytes(self.storage[offset:offset+4], "little")

    def _record(self, kind, **fields):
        self.events.append({"kind": kind, **fields})
        del self.events[:-64]

    def read(self, offset, size):
        self._access(offset, size)
        word = offset & ~3
        value = self._word(word)
        if self.mode == "pending":
            if self.read_layout and word == self.read_layout.status_offset:
                value = self.read_status
            elif self.read_layout and word == self.read_layout.clear_offset:
                value = 0  # write-one-to-clear command register
            for bank, base in enumerate(self.banks):
                if word == base + 8:
                    value = int(bank not in self.pending)
        return (value >> ((offset & 3)*8)) & ((1 << (size*8))-1)

    def write(self, offset, size, value):
        self._access(offset, size)
        if type(value) is not int or not 0 <= value < (1 << (size*8)):
            raise ValueError("BSI value does not fit access width")
        if self.mode == "pending" and self.read_layout:
            word = offset & ~3
            if word == self.read_layout.status_offset:
                self._record("readonly_write_ignored", offset=offset)
                return True
            if word == self.read_layout.clear_offset:
                if size != 4:
                    raise NotImplementedError("Partial BSI read-status clear needs a reviewed ABI")
                self.read_status &= ~value
                self._record("read_status_cleared", mask=value)
                return True
        bank = next((i for i, b in enumerate(self.banks) if b <= offset < b + 0x20), None)
        relative = None if bank is None else (offset - self.banks[bank]) & ~3
        if self.mode == "pending" and relative in (8, 12, 16):
            self._record("readonly_write_ignored", offset=offset)
            return True
        if relative == 0 and self.mode == "pending":
            if size != 4:
                raise NotImplementedError("Partial BSI command writes need a reviewed ABI")
            if bank in self.pending:
                self.busy_rejections += 1
                self._record("busy_write_rejected", bank=bank)
                return True
        self.storage[offset:offset+size] = value.to_bytes(size, "little")
        if bank is not None:
            self._record("write", offset=offset, size=size, value=value)
        if relative == 0 and size == 4 and value & 1:
            base = self.banks[bank]
            self.sequence += 1
            command = SerialCommand(self.sequence, bank, (value >> 8) & 15,
                bool(value & 2), bool(value & 4),
                (self._word(base+4), self._word(base+0x18)),
                (self._word(base+0x14), self._word(base+0x1c)))
            self._record("command", **asdict(command))
            if self.mode == "pending":
                self.pending[bank] = command
                self._dispatch(command)
        return True

    def _dispatch(self, command):
        if self.serial_bus is None:
            self._unresolved(command, "no-backend-connected")
            return
        # Never consume another target read when the controller cannot publish
        # it. Polling does not retry dispatch or mutate either endpoint.
        if command.extended or (command.read and (self.read_layout is None or
                self.read_status & (1 << self.read_layout.bank_ready_bits[command.bank]))):
            self._unresolved(command, "unsupported-transfer-or-unacknowledged-read")
            return
        result = self.serial_bus.exchange(command)
        if result.status == "unresolved":
            if result.value is not None:
                raise ValueError("Unresolved target result must not supply data")
            self._unresolved(command, result.reason)
        elif result.status == "write-complete" and not command.read and result.value is None:
            self.complete_write(command.bank, command.sequence)
        elif result.status == "read-complete" and command.read:
            self.complete_read(command.bank, command.sequence, result.value)
        else:
            raise ValueError("Serial backend returned an incompatible completion")

    def _unresolved(self, command, reason):
        # Diagnostic state is bounded by the pending bank count, not the event
        # ring. Retain the original decision even if rejected retries rotate
        # that ring. Nothing here retries, completes or changes a transaction.
        fields = dict(bank=command.bank, sequence=command.sequence, reason=reason)
        self.pending_blockers[command.bank] = fields
        self._record("backend_unresolved", **fields)

    def complete_write(self, bank, sequence):
        """Explicit future backend boundary; NEVER called by guest polling.

        Reads use complete_read with an explicitly supplied payload and layout.
        A backend must call this only after actually handling the transaction.
        """
        if self.mode != "pending" or bank not in self.pending:
            raise ValueError("No pending BSI command")
        command = self.pending[bank]
        if command.sequence != sequence:
            raise ValueError("Stale BSI completion")
        if command.read or command.extended:
            raise NotImplementedError("RF read/extended completion is unsupported")
        del self.pending[bank]
        self.pending_blockers.pop(bank, None)
        self.completed += 1
        # The explicit backend completion returns this channel to idle.
        self.storage[self.banks[bank]] &= ~1
        self._record("backend_write_complete", bank=bank, sequence=sequence)

    def complete_read(self, bank, sequence, value):
        """Publish an explicit backend's 36-bit result; never generate RF data.

        Unread results cannot be overwritten. Extended transfers and partial
        status-clear writes remain unsupported. No poll or timer calls this.
        """
        if self.read_layout is None:
            raise NotImplementedError("No BSI read-completion ABI selected")
        if self.mode != "pending" or bank not in self.pending:
            raise ValueError("No pending BSI command")
        command = self.pending[bank]
        if command.sequence != sequence:
            raise ValueError("Stale BSI completion")
        if not command.read or command.extended:
            raise NotImplementedError("Expected a non-extended BSI read")
        if type(value) is not int or not 0 <= value < 1 << 36:
            raise ValueError("BSI read result must fit 36 bits")
        bit = 1 << self.read_layout.bank_ready_bits[bank]
        if self.read_status & bit:
            raise ValueError("Previous BSI read result has not been acknowledged")
        base = self.banks[bank]
        self.storage[base+12:base+16] = (value & 0xffffffff).to_bytes(4, "little")
        self.storage[base+16:base+20] = (value >> 32).to_bytes(4, "little")
        self.read_status |= bit
        del self.pending[bank]
        self.pending_blockers.pop(bank, None)
        self.storage[base] &= ~1
        self.completed_reads += 1
        self._record("backend_read_complete", bank=bank, sequence=sequence, value=value)

    def facts(self):
        return {"abi": "mt6768-bsi-immediate/v1", "mode": self.mode,
            "analysis_only": True, "firmware_boot_verified": False,
            "reset_policy": "zero-RAM" if self.mode == "observe" else "empty-controller-ready",
            "reset_silicon_verified": False, "backend_connected": self.serial_bus is not None,
            "serial_targets": self.serial_bus.facts() if self.serial_bus is not None else {},
            "rf_emulated": False, "dsp_emulated": False,
            "commands": self.sequence, "completed_writes": self.completed,
            "completed_reads": self.completed_reads, "read_status": self.read_status,
            "read_completion_layout": asdict(self.read_layout) if self.read_layout else None,
            "busy_rejections": self.busy_rejections,
            "pending": [asdict(c) for c in self.pending.values()],
            "pending_blockers": [dict(item) for item in self.pending_blockers.values()],
            "events": list(self.events), "unmodelled_registers": "RAM-compatible storage"}
