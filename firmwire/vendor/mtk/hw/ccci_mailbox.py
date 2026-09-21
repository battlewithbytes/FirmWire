"""Strict CCCI mailbox framing and explicit service routing.

No platform selection, guest pointers, MMIO, or default successful responses.
Handlers own service semantics; PCCIF owns queueing. No IRQ is invented here.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import struct


def _unsigned(value, bits, name):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError("Invalid mailbox " + name)


@dataclass(frozen=True)
class MailboxMessage:
    channel: int
    message_id: int
    parameter: int = field(default=0, repr=False)
    auxiliary: int = 0

    def __post_init__(self):
        _unsigned(self.channel, 16, "channel")
        _unsigned(self.message_id, 32, "message ID")
        _unsigned(self.parameter, 32, "parameter")
        _unsigned(self.auxiliary, 16, "auxiliary word")

    def encode(self):
        return struct.pack("<IIHHI", 0xffffffff, self.message_id,
                           self.channel, self.auxiliary, self.parameter)


def decode_mailbox(packet):
    """Decode the selected 16-byte mailbox ABI; never dereference reserved."""
    if len(packet) != 16:
        raise ValueError("CCCI mailbox requires exactly 16 bytes")
    magic, message_id, channel, auxiliary, parameter = struct.unpack("<IIHHI", packet)
    if magic != 0xffffffff:
        raise ValueError("Unsupported CCCI mailbox framing")
    return MailboxMessage(channel, message_id, parameter, auxiliary)


@dataclass(frozen=True)
class MailboxResult:
    disposition: str
    response: MailboxMessage | None = None

    def __post_init__(self):
        if not isinstance(self.disposition, str) or not self.disposition.strip():
            raise ValueError("Mailbox handler requires an explicit disposition")
        if self.response is not None and not isinstance(self.response, MailboxMessage):
            raise ValueError("Mailbox response must be a validated message")


class MailboxEndpoint(ABC):
    @abstractmethod
    def receive(self, message):
        """Return MailboxResult; a request never implies an automatic reply."""


class MailboxDispatcher:
    def __init__(self, routes):
        """routes is an iterable of (channel, message_id, endpoint) triples.

        An iterable, not a dict, makes duplicate registrations detectable.
        There are deliberately no wildcard routes or permissive fallback.
        """
        self._routes = {}
        for channel, message_id, endpoint in routes:
            _unsigned(channel, 16, "route channel")
            _unsigned(message_id, 32, "route message ID")
            if not isinstance(endpoint, MailboxEndpoint):
                raise ValueError("Mailbox routes require explicit endpoints")
            if (channel, message_id) in self._routes:
                raise ValueError("Duplicate mailbox route")
            self._routes[channel, message_id] = endpoint
        self.channels = frozenset(channel for channel, _ in self._routes)

    def accepts(self, channel):
        return channel in self.channels

    def validate_channels(self, occupied):
        if self.channels.intersection(occupied):
            raise ValueError("Mailbox channel collides with another CCCI service")

    def receive(self, packet):
        message = decode_mailbox(packet)
        endpoint = self._routes.get((message.channel, message.message_id))
        if endpoint is None:
            raise NotImplementedError("Unsupported CCCI mailbox route")
        result = endpoint.receive(message)
        if not isinstance(result, MailboxResult):
            raise TypeError("Mailbox endpoint must return MailboxResult")
        return result
