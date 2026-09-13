"""Standalone AP property service, not a connection to a running Android AP.

Wire contract: ALPS Q0 ccci_rpcd_com/ccci_rpcd.c, query 0x400f.
Values must come from an explicit profile; never read the host environment.
An absent property has Android property_get's empty-value/zero-length result.
"""
import struct


class APSystemProperties:
    def __init__(self, values=None, *, source="standalone-empty-property-store"):
        if not isinstance(source, str) or not source.strip():
            raise ValueError("AP property source must be identified")
        if values and source == "standalone-empty-property-store":
            raise ValueError("Configured AP properties require explicit provenance")
        self.source = source
        self.values = dict(values or {})
        for name, value in self.values.items():
            if not isinstance(name, str) or not name or "\x00" in name:
                raise ValueError("Invalid AP property name")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Invalid AP property value")
            # This legacy RPC uses a PROPERTY_VALUE_MAX (92) byte buffer,
            # even on Android versions which permit longer read-only values.
            if len(value.encode("utf-8")) >= 92:
                raise ValueError("AP property exceeds the legacy RPC value buffer")

    def query(self, packets):
        if len(packets) != 1 or not packets[0]:
            raise ValueError("AP property RPC requires one nonempty name")
        raw_name = packets[0].rstrip(b"\x00")
        if not raw_name or b"\x00" in raw_name:
            raise ValueError("AP property name has an embedded NUL")
        name = raw_name.decode("utf-8")
        value = self.values.get(name, "").encode("utf-8")
        return name, name in self.values, [struct.pack("<i", len(value)), value + b"\x00"]
