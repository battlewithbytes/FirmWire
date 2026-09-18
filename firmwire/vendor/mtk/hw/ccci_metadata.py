"""Bounded, payload-free CCCI packet metadata; never decides how to reply."""
import struct


def packet_metadata(packet):
    """Describe the existing 16-byte header without dumping data or reserved words.

    data1 is a length only for the zero-data0 stream framing. Mailbox and
    unclassified headers retain an unresolved length, not a false mismatch.
    The upper channel halfword is retained without inventing its semantics.
    """
    result = {"packet_bytes": len(packet), "header_complete": len(packet) >= 16,
              "channel": None, "channel_aux": None, "header_kind": "truncated",
              "declared_stream_length": None, "stream_length_matches": None}
    if len(packet) >= 16:
        data0, data1, channel, auxiliary = struct.unpack_from("<IIHH", packet)
        result.update(channel=channel, channel_aux=auxiliary,
                      header_kind="stream" if data0 == 0 else
                                  "mailbox" if data0 == 0xffffffff else "unclassified")
        if data0 == 0:
            result.update(declared_stream_length=data1, stream_length_matches=data1 == len(packet))
    return result
