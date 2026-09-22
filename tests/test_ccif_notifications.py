"""Transport facts, independent of a particular modem or CPU IRQ source."""
import logging
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from firmwire.hw.channel_doorbell import ChannelDoorbell
from firmwire.hw.soc import SOCPeripheral
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph, Ringbuf
from firmwire.vendor.mtk.loader import MTKLoader


class CCIFNotificationTests(unittest.TestCase):
    def memory(self):
        mem = bytearray(280)
        struct.pack_into("<6I", mem, 0, 0, 0, 128, 0, 0, 128)
        return SimpleNamespace(mem=mem, offsets=[0],
            read_raw=lambda offset, size: int.from_bytes(mem[offset:offset+size], "little"),
            write_raw=lambda offset, size, value: mem.__setitem__(slice(offset,offset+size), value.to_bytes(size,"little")))

    def device(self, enabled=True):
        device = object.__new__(PCCIF_Periph)
        device.mem = [0] * 4096
        device.log = logging.getLogger("ccif-notification-test")
        device.pccifid = 0
        device.rchnum = 1 << 15  # Independent legacy SRAM notification.
        device.reply_doorbell = ChannelDoorbell(8) if enabled else None
        return device

    def test_notification_is_after_commit_and_failed_enqueue_does_not_signal(self):
        parent = self.memory()
        committed = []
        ring = Ringbuf(parent, 0, on_reply=lambda: committed.append(parent.read_raw(16, 4)))
        self.assertTrue(ring.writePacket(bytes(16)))
        self.assertEqual(committed, [32])
        with self.assertRaises(AssertionError): ring.writePacket(bytes(128))
        self.assertEqual(committed, [32])

    def test_bits_are_guest_visible_and_ack_does_not_consume_queue(self):
        device = self.device()
        parent = self.memory()
        ring = Ringbuf(parent, 0, on_reply=lambda: device.reply_doorbell.notify(4))
        ring.writePacket(bytes(16))
        ring.writePacket(bytes(16))
        self.assertEqual(device.hw_read(0x10, 4), (1 << 15) | 16)
        before = bytes(parent.mem)
        self.assertTrue(device.hw_write(0x14, 4, 16))
        self.assertEqual(device.hw_read(0x10, 4), 1 << 15)
        self.assertEqual(bytes(parent.mem), before)
        self.assertEqual(device.reply_doorbell.snapshot()["coalesced"][4], 1)
        ring.writePacket(bytes(16))
        self.assertEqual(device.hw_read(0x10, 4), (1 << 15) | 16)
        device.hw_write(0x14, 4, (1 << 15) | 16)
        self.assertEqual(device.hw_read(0x10, 4), 0)

    def test_default_transport_does_not_enable_bits_or_cpu_interrupts(self):
        device = self.device(False)
        parent = self.memory()
        Ringbuf(parent, 0).writePacket(bytes(16))
        self.assertEqual(device.hw_read(0x10, 4), 1 << 15)
        self.assertIsNone(device.reply_doorbell)

    def test_tx_dispatch_attaches_channel_not_application_message_id(self):
        device = self.device()
        parent = self.memory()
        device.ringbuffer = parent
        with unittest.mock.patch.object(Ringbuf, "readPacket", side_effect=[b"packet", None]):
            device.dispatch_ccci_packet = lambda ring, packet, metadata: ring.writePacket(bytes(16))
            self.assertTrue(device.hw_write(0xc, 4, 0))
        self.assertEqual(device.reply_doorbell.pending, 1)

    def test_loader_requires_explicit_native_abi_and_one_transport(self):
        peripheral = SOCPeripheral(PCCIF_Periph, 0x123000, 0x1000, pccifid=0)
        abi = "ring-index-channel-bit-analysis"
        for mode, targets, selection, valid in (
                ("native", [peripheral], abi, True),
                ("rehosted", [peripheral], abi, False),
                ("native", [], abi, False),
                ("native", [peripheral] * 2, abi, False),
                ("native", [peripheral], "guessed", False),
                ("native", [peripheral], "disabled", True)):
            with self.subTest(mode=mode, targets=len(targets), selection=selection):
                loader = object.__new__(MTKLoader)
                loader.loader_args = {"ccif_notifications": selection}
                loader.boot_mode, loader.capability_report = mode, {}
                loader.modem_soc = SimpleNamespace(peripherals=targets)
                loader.build_peripheral_maps, loader.create_peripheral = Mock(), Mock()
                loader.write_capability_report = Mock()
                if valid:
                    loader.build_memory_map()
                    kwargs = loader.create_peripheral.call_args.kwargs
                    if selection == abi:
                        self.assertEqual(kwargs["reply_notifications"], abi)
                        facts = loader.capability_report["ccif_notifications"]
                        self.assertTrue(facts["analysis_only"])
                        self.assertFalse(facts["cpu_interrupt_connected"])
                        self.assertFalse(facts["application_delivery_verified"])
                    else:
                        self.assertNotIn("reply_notifications", kwargs)
                        self.assertNotIn("ccif_notifications", loader.capability_report)
                else:
                    with self.assertRaises(ValueError): loader.build_memory_map()
                    loader.build_peripheral_maps.assert_not_called()
                self.assertNotIn("reply_notifications", peripheral._attr)
