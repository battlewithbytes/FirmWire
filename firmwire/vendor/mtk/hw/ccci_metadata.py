"""Bounded, payload-free CCCI packet metadata; never decides how to reply."""
import struct


def packet_metadata(packet):
    """Describe the existing 16-byte header without dumping payload or reserved words.

    A zero data0 gives a candidate stream-length comparison, not a validity
    gate: data1 can have other meanings in channel-specific contracts. Mailbox
    and unclassified headers retain an unresolved length, not a false mismatch.
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
        elif data0 == 0xffffffff:
            # Mailbox data1 is an opaque command ID, not a stream length.
            # Parameters in reserved remain private and are never dereferenced.
            result["mailbox_message_id"] = data1
    return result


def ipc_metadata(packet):
    """Read the reviewed 32-bit ILM envelope; parameter bytes stay opaque.

    This is diagnostic evidence, not automatic service selection. Pointer
    fields are presence observations only and are never dereferenced/logged.
    Inline local-parameter length is a candidate until the peer ABI is reviewed.
    """
    result = {"schema": "ccci-ipc-ilm32-metadata/v1", "ilm_complete": False,
              "application_verified": False}
    if len(packet) < 40:
        return result
    if struct.unpack_from("<H", packet, 8)[0] != 0x22:
        raise ValueError("IPC metadata requires MD-to-AP IPC channel")
    source, destination, sap, message, local_pointer, peer_pointer = struct.unpack_from("<6I", packet, 16)
    result.update(ilm_complete=True, ap_unified_id=struct.unpack_from("<I",packet,12)[0],
                  source_module=source, destination_module=destination,
                  sap_id=sap, message_id=message,
                  wire_local_pointer_nonzero=bool(local_pointer),
                  wire_peer_pointer_nonzero=bool(peer_pointer),
                  bytes_after_ilm=len(packet)-40,
                  local_parameter_length_candidate=None,
                  local_parameter_length_matches=None)
    if len(packet) >= 44:
        local_length = struct.unpack_from("<H",packet,42)[0]
        result.update(local_parameter_length_candidate=local_length,
                      local_parameter_length_matches=local_length == len(packet)-40)
    return result
