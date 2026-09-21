"""Software TX-power state query, separate from mailbox transport and RF.

The legacy MD1 ABI is selected explicitly, never inferred from a ROM/ISA.
See Cockpit docs/2026-09-21-ccci-mailbox.md for the reviewed AP reference.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .ccci_mailbox import MailboxEndpoint, MailboxMessage, MailboxResult


def _mode(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("SWTP pin state must be explicit 0 or 1")
    return value


class TxPowerState(ABC):
    @abstractmethod
    def read_mode(self):
        """Provide a reviewed ABI pin state, not an RF power measurement."""


class FixedTxPowerState(TxPowerState):
    def __init__(self, mode):
        self._mode = _mode(mode)

    def read_mode(self):
        return self._mode


@dataclass(frozen=True)
class SoftwareTxPowerABI:
    request_channel: int
    request_id: int
    response_channel: int
    response_id: int

    def __post_init__(self):
        MailboxMessage(self.request_channel, self.request_id)
        MailboxMessage(self.response_channel, self.response_id)


LEGACY_MD1_SWTP = SoftwareTxPowerABI(2, 0x110, 3, 0x10e)


class SoftwareTxPowerEndpoint(MailboxEndpoint):
    def __init__(self, abi, state):
        if not isinstance(abi, SoftwareTxPowerABI) or not isinstance(state, TxPowerState):
            raise ValueError("SWTP requires an explicit ABI and state provider")
        self.abi, self.state = abi, state

    def receive(self, message):
        if (message.channel, message.message_id) != (self.abi.request_channel, self.abi.request_id):
            raise NotImplementedError("Unsupported SWTP query")
        # The AP callback ignores the request parameter. Never echo it or use it
        # as an address. Sequence/auxiliary bits are not copied into the reply.
        mode = _mode(self.state.read_mode())
        return MailboxResult("software-tx-power-state-analysis", MailboxMessage(
            self.abi.response_channel, self.abi.response_id, mode))
