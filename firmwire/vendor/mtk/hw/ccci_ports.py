"""Explicit unopened AP character ports, not application emulation."""
import struct


class ClosedAPPorts:
    def __init__(self, profile):
        if not isinstance(profile, dict) or profile.get("schema") != "firmwire.ccci-closed-ports/v1":
            raise ValueError("Unsupported closed-port profile schema")
        for key in ("profile", "source"):
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                raise ValueError("Closed-port policy requires identity and provenance")
        entries = profile.get("channels")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
            raise ValueError("Closed-port profile requires 1..64 channels")
        self.profile, self.source = profile["profile"], profile["source"]
        self.channels = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid closed-port descriptor")
            channel, limit, name = entry.get("channel"), entry.get("max_packet_bytes"), entry.get("name")
            if type(channel) is not int or not 0 <= channel <= 0xffff:
                raise ValueError("Invalid closed-port channel")
            if channel in (0, 0xe, 0x20, 0x22) or channel in self.channels:
                raise ValueError("Closed port collides with an existing service")
            if type(limit) is not int or not 16 <= limit <= 65536:
                raise ValueError("Invalid closed-port packet bound")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Closed port needs a reviewed name")
            self.channels[channel] = (name, limit)
        self.dropped = {channel: 0 for channel in self.channels}

    def accepts(self, channel):
        return channel in self.channels

    def receive(self, packet):
        if len(packet) < 16:
            raise ValueError("Truncated closed-port header")
        channel = struct.unpack_from("<H", packet, 8)[0]
        if not self.accepts(channel):
            raise NotImplementedError("Unconfigured AP character port")
        name, limit = self.channels[channel]
        if len(packet) > limit:
            raise ValueError("Closed-port packet exceeds profile bound")
        # The AP's usage-count-zero path drops opaque input, without decoding
        # application data or returning an application reply. Ring validation
        # is the transport's responsibility; data0/data1 are channel-specific.
        self.dropped[channel] = min(self.dropped[channel] + 1, (1 << 64) - 1)
        return {"policy": "unopened-ap-character-port/v1", "profile": self.profile,
                "port": name, "disposition": "dropped-port-unopened",
                "dropped": self.dropped[channel], "peer_connected": False,
                "application_verified": False, "response_sent": False}

    def facts(self):
        return {"schema": "firmwire.ccci-closed-ports/v1", "profile": self.profile,
                "source": self.source, "channels": sorted(self.channels),
                "peer_connected": False, "application_verified": False,
                "response_supported": False}
