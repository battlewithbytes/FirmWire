"""Explicit IPC ABI and endpoint policy, separate from CCIF transport.

No ROM/PC offsets, guest-pointer dereferences, or implicit successful replies.
The initial endpoint models ALPS Q0 WMT with STP unavailable, not a running AP.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import struct


@dataclass(frozen=True)
class IPCMessage:
    ap_unified_id: int
    source_module: int
    destination_module: int
    sap_id: int
    message_id: int
    local_parameter: bytes = field(repr=False)


def decode_ilm32(packet, *, max_local_parameter=1024):
    """Reviewed MD-to-AP inline-local-parameter ABI; no peer buffer support."""
    if type(max_local_parameter) is not int or max_local_parameter < 4:
        raise ValueError("Invalid IPC parameter limit")
    if not 44 <= len(packet) <= 40 + max_local_parameter:
        raise ValueError("IPC envelope size is outside the selected ABI")
    data0, length, channel, auxiliary, ap_id = struct.unpack_from("<IIHHI", packet)
    if data0 != 0 or length != len(packet) or channel != 0x22:
        raise ValueError("Unsupported IPC stream header")
    source, destination, sap, message, local_pointer, peer_pointer = struct.unpack_from("<6I", packet, 16)
    if peer_pointer:
        raise NotImplementedError("IPC peer buffers are not supported")
    # Wire pointers are opaque: AP reconstructs the inline parameter locally.
    local_length = struct.unpack_from("<H", packet, 42)[0]
    if local_length != len(packet) - 40:
        raise ValueError("IPC inline parameter length mismatch")
    return IPCMessage(ap_id, source, destination, sap, message, bytes(packet[40:]))


class IPCEndpoint(ABC):
    @abstractmethod
    def receive(self, message):
        """Return a disposition, never a transport acknowledgment."""


class UnavailableConnectivityEndpoint(IPCEndpoint):
    """ALPS WMT's STP-not-ready branch drops notifications without a reply."""
    def __init__(self):
        self.dropped = 0

    def receive(self, message):
        self.dropped = min(self.dropped + 1, (1 << 64) - 1)
        return {"policy": "wmt-stp-unavailable-analysis/v1",
                "disposition": "dropped-peer-unavailable", "dropped": self.dropped,
                "peer_connected": False, "application_verified": False,
                "response_sent": False}


class IPCDispatcher:
    def __init__(self, routes, *, max_local_parameter=1024):
        self.routes = dict(routes)
        for route, endpoint in self.routes.items():
            if (not isinstance(route, tuple) or len(route) != 2 or
                    any(type(value) is not int or not 0 <= value <= 0xffffffff for value in route)):
                raise ValueError("IPC route must identify unified AP and destination module IDs")
            if not isinstance(endpoint, IPCEndpoint):
                raise ValueError("IPC routes require explicit endpoints")
        if type(max_local_parameter) is not int or max_local_parameter < 4:
            raise ValueError("Invalid IPC parameter limit")
        self.max_local_parameter = max_local_parameter

    def receive(self, packet):
        message = decode_ilm32(packet, max_local_parameter=self.max_local_parameter)
        route = (message.ap_unified_id, message.destination_module)
        if route not in self.routes:
            raise NotImplementedError("Unsupported IPC destination route")
        return self.routes[route].receive(message)


def unavailable_wmt_dispatcher():
    # Reviewed sibling MOLY route, selected explicitly, never inferred from ISA.
    return IPCDispatcher({(0x80000003, 0x404): UnavailableConnectivityEndpoint()})
