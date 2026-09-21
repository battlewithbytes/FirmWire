import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from firmwire.hw.soc import SOCPeripheral
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph
from firmwire.vendor.mtk.hw.ccci_mailbox import MailboxMessage, MailboxDispatcher
from firmwire.vendor.mtk.hw.ccci_swtp import (
    SoftwareTxPowerABI, SoftwareTxPowerEndpoint, FixedTxPowerState, TxPowerState,
)
from firmwire.vendor.mtk.ccci_mailbox_profile import build_mailbox_profile
from firmwire.vendor.mtk.loader import MTKLoader


def profile(mode=0):
    return {"schema": "firmwire.ccci-mailbox-profile/v1", "profile": "test",
            "source": "synthetic test", "services": [{"kind": "legacy-md1-swtp-analysis/v1", "mode": mode}]}


class SWTPTests(unittest.TestCase):
    def test_selected_abi_and_both_pin_states(self):
        for mode in (0, 1):
            dispatcher, facts = build_mailbox_profile(profile(mode))
            for parameter in (0, 0xdeadbeef):
                result = dispatcher.receive(MailboxMessage(2, 0x110, parameter, 0xffff).encode())
                self.assertEqual(result.response.encode(), MailboxMessage(3, 0x10e, mode).encode())
            self.assertTrue(facts["analysis_only"])
            self.assertFalse(facts["peer_connected"])
            self.assertFalse(facts["guest_delivery_verified"])
            for command in (0x10e, 0x111, 0x105, 0x119):
                with self.assertRaises(NotImplementedError):
                    dispatcher.receive(MailboxMessage(2, command).encode())

    def test_new_abi_and_live_provider_do_not_need_transport_changes(self):
        class State(TxPowerState):
            value = 1
            def read_mode(self): return self.value
        state = State()
        endpoint = SoftwareTxPowerEndpoint(SoftwareTxPowerABI(42, 700, 43, 701), state)
        request = MailboxMessage(42, 700, 1234)
        self.assertEqual(endpoint.receive(request).response, MailboxMessage(43, 701, 1))
        state.value = 0
        self.assertEqual(endpoint.receive(request).response.parameter, 0)
        state.value = 9
        with self.assertRaises(ValueError): endpoint.receive(request)
        with self.assertRaises(NotImplementedError): endpoint.receive(MailboxMessage(2, 0x110))
        with self.assertRaises(ValueError): SoftwareTxPowerEndpoint(None, state)
        with self.assertRaises(ValueError): SoftwareTxPowerEndpoint(endpoint.abi, None)

    def test_strict_profiles_no_implicit_state_or_unknown_services(self):
        invalid = [None, {}, dict(profile(), extra=0), dict(profile(), source=""),
                   dict(profile(), schema="unknown"), dict(profile(), services=[]),
                   dict(profile(), services=profile()["services"]*2)]
        invalid += [profile(mode) for mode in (None, True, -1, 2, "0")]
        invalid += [dict(profile(), services=[entry]) for entry in (
            {}, {"kind":"unknown"}, {"kind":"legacy-md1-swtp-analysis/v1"},
            {"kind":"legacy-md1-swtp-analysis/v1","mode":0,"reply":123}, None)]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError): build_mailbox_profile(item)

    def test_loader_validation_native_only_and_copied_attributes(self):
        peripheral = SOCPeripheral(PCCIF_Periph, 0x123000, 0x1000, pccifid=0)
        for mode, targets, valid in (("native", [peripheral], True),
                                      ("rehosted", [peripheral], False),
                                      ("native", [], False), ("native", [peripheral]*2, False)):
            loader = object.__new__(MTKLoader)
            loader.loader_args = {"ccci_mailbox_profile": "profile.json"}
            loader.boot_mode, loader.capability_report = mode, {}
            loader.modem_soc = SimpleNamespace(peripherals=targets)
            loader.build_peripheral_maps, loader.create_peripheral = Mock(), Mock()
            loader.write_capability_report = Mock()
            with patch("builtins.open", mock_open(read_data=json.dumps(profile()).encode())):
                if valid:
                    loader.build_memory_map()
                    self.assertIsInstance(loader.create_peripheral.call_args.kwargs["mailbox_dispatcher"], MailboxDispatcher)
                    self.assertEqual(len(loader.capability_report["ccci_mailbox"]["profile_sha256"]), 64)
                else:
                    with self.assertRaises(ValueError): loader.build_memory_map()
                    loader.build_peripheral_maps.assert_not_called()
            self.assertNotIn("mailbox_dispatcher", peripheral._attr)

    def test_conflicting_closed_port_fails_before_mapping(self):
        loader = object.__new__(MTKLoader)
        loader.loader_args = {"ccci_mailbox_profile":"mailbox.json", "ccci_closed_ports":"ports.json"}
        loader.boot_mode = "native"
        loader.build_peripheral_maps = Mock()
        ports = {"schema":"firmwire.ccci-closed-ports/v1", "profile":"test", "source":"test",
                 "channels":[{"channel":2,"name":"collision","max_packet_bytes":16}]}
        with patch("builtins.open", mock_open(read_data=b"{}")), patch("json.load", return_value=ports), \
                patch("json.loads", return_value=profile()), \
                self.assertRaisesRegex(ValueError, "collides"):
            loader.build_memory_map()
        loader.build_peripheral_maps.assert_not_called()
