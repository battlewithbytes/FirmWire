"""WACS transport adapter for an explicitly shared PMIC target."""
from .pmic import PmicTarget, check_result, uint


class PmicWacsControl:
    """Strict opt-in WACS transport. Polling never retries or supplies data.

    FSM 0=idle, 2=request pending, 6=read valid until explicit clear. The layout
    is the reviewed legacy WACS word encoding, not a universal PMIC protocol.
    """
    def __init__(self, target, *, init_done_offset=21):
        if (not isinstance(target, PmicTarget) or type(init_done_offset) is not int
                or not 21 <= init_done_offset < 32):
            raise ValueError("WACS requires a PMIC target and a nonoverlapping init bit")
        self.target, self.init_done_offset = target, init_done_offset
        self.reset()

    def reset(self):
        self.state = self.data = self.completed_reads = self.completed_writes = 0
        self.pending = None
        self.blocker = None

    def read(self, offset):
        if type(offset) is not int or offset != 4:
            raise ValueError("Unsupported strict WACS read register")
        return self.data | self.state << 16 | 1 << self.init_done_offset

    def write(self, offset, value):
        if type(offset) is not int or not uint(value, 32):
            raise ValueError("Invalid strict WACS word access")
        if offset == 8:
            if value != 1 or self.state != 6:
                raise ValueError("WACS valid clear requires a completed read and value one")
            self.state = self.data = 0
            return
        if offset != 0 or self.state != 0:
            raise ValueError("WACS command register unavailable")
        # Write flag is not part of the 15-bit, halfword-address field.
        self.pending = dict(read=not bool(value >> 31),
                            address=((value >> 16) & 0x7fff) << 1, value=value & 0xffff)
        self.state = 2
        self.retry_pending()

    def retry_pending(self):
        """Explicit backend retry only; no progress driven by guest polls."""
        if self.pending is None:
            raise ValueError("No pending WACS request")
        request = self.pending
        read = request["read"]
        reply = (self.target.read(request["address"]) if read else
                 self.target.write(request["address"], request["value"]))
        reply = check_result(reply, read=read)
        if reply.status == "unresolved":
            self.blocker = reply.reason
            return False
        self.pending = self.blocker = None
        if read:
            self.data, self.state = reply.value, 6
            self.completed_reads += 1
        else:
            self.state = self.data = 0
            self.completed_writes += 1
        return True

    def facts(self):
        return dict(kind="shared-pmic-wacs-analysis/v1", state=self.state, data=self.data,
                    pending=dict(self.pending) if self.pending is not None else None,
                    blocker=self.blocker, completed_reads=self.completed_reads,
                    completed_writes=self.completed_writes, polling_advances=False)
