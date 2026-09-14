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


class BsiImmediateControl:
    def __init__(self, size=0x9000, bank_offsets=(0x1000, 0x1100), mode="observe"):
        if mode not in ("observe", "pending"):
            raise ValueError("Unknown BSI analysis mode")
        if type(size) is not int or not 0x20 <= size <= 0x100000:
            raise ValueError("Invalid BSI bank size")
        if (not 1 <= len(bank_offsets) <= 8 or len(set(bank_offsets)) != len(bank_offsets)
                or any(type(b) is not int or b < 0 or b % 4 or b + 0x20 > size for b in bank_offsets)
                or any(abs(a-b) < 0x20 for i, a in enumerate(bank_offsets) for b in bank_offsets[i+1:])):
            raise ValueError("Invalid or overlapping BSI banks")
        self.size, self.banks, self.mode = size, tuple(bank_offsets), mode
        self.reset()

    def reset(self):
        self.storage = bytearray(self.size)
        self.pending = {}
        self.sequence = self.completed = self.busy_rejections = 0
        self.events = []

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
            for bank, base in enumerate(self.banks):
                if word == base + 8:
                    value = int(bank not in self.pending)
        return (value >> ((offset & 3)*8)) & ((1 << (size*8))-1)

    def write(self, offset, size, value):
        self._access(offset, size)
        if type(value) is not int or not 0 <= value < (1 << (size*8)):
            raise ValueError("BSI value does not fit access width")
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
        return True

    def complete_write(self, bank, sequence):
        """Explicit future backend boundary; NEVER called by guest polling.

        Reads and extended transfers need their own reviewed completion ABI.
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
        self.completed += 1
        # The explicit backend completion returns this channel to idle.
        self.storage[self.banks[bank]] &= ~1
        self._record("backend_write_complete", bank=bank, sequence=sequence)

    def facts(self):
        return {"abi": "mt6768-bsi-immediate/v1", "mode": self.mode,
            "analysis_only": True, "firmware_boot_verified": False,
            "reset_policy": "zero-RAM" if self.mode == "observe" else "empty-controller-ready",
            "reset_silicon_verified": False, "backend_connected": False,
            "rf_emulated": False, "dsp_emulated": False,
            "commands": self.sequence, "completed_writes": self.completed,
            "busy_rejections": self.busy_rejections,
            "pending": [asdict(c) for c in self.pending.values()],
            "events": list(self.events), "unmodelled_registers": "RAM-compatible storage"}
