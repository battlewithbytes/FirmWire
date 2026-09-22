# Read-only CCCI ring evidence

Select the actual `SHM_CCIF_Periph` instance's name in an explicit, ROM-bound
RAM observer profile's `peripheral_controls` list. Names are not hardcoded:
the machine adapter accepts only instances of that peripheral class.
Observation is off by default and enabling it twice preserves existing counts.

`hw/ccci_ring_observer.py` owns the reusable observer. Ring offsets come from
the instantiated normal and exception queue layouts, not firmware PCs, image
hashes, inferred CPU addresses, or one modem's constants. The existing Python
shared-memory peripheral remains the backing store. Its forwarded guest writes
go through `hw_write`; emulator response writes use `write_raw` and are counted
separately after `Ringbuf.writePacket` finishes a frame.

The `firmwire.ccci-ring-observation/v1` facts expose:

- Both directions' read/write/capacity words and validity of their bounds.
- Modulo pending bytes in the AP-to-MD direction, or null for invalid geometry.
- Emulator queued-frame counts and guest writes overlapping the reply read cursor.
- The last 16 producer/read-cursor events, without copying packet payloads.
- A bounded cursor advance only for an aligned, full-word guest cursor write
  with valid unchanged geometry and producer position, advancing no farther
  than the previously queued bytes. Split, invalid and inconsistent writes
  retain evidence but report null advance.

Zero-sized unused rings are reported invalid for transport arithmetic, not as
a guest failure. Equal cursors cannot distinguish no movement from a full cycle;
cursor reset is not distinguishable from acknowledgement using positions alone.
Even a plausible read advance does not prove the application interpreted a reply.
The output always retains `application_verified: false` and
`interrupt_delivery_verified: false`. It does not inject IRQs, alter queue bytes,
ACK the modem, model an AP, or certify boot.

Tests exercise every aligned wrap position, independent normal/exception queues,
malformed capacities/cursors, partial writes, reset-like jumps, default-off
behavior, bounded history, repeated enabling, read-only snapshots, and typed
adapter selection under a relocated device name.
