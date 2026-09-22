import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from firmwire.hw.irq_router import ChannelIRQRouter
from firmwire.hw.channel_doorbell import ChannelDoorbell
from firmwire.hw.routed_irq import RoutedLevelIRQController
from firmwire.emulator.mips_irq import DeferredIRQOutputs
from firmwire.vendor.mtk.hw.mdcirq_delivery import MdcirqLevelDelivery
from firmwire.vendor.mtk.hw.MDCPeripheral import MDCIRQ_Periph
from firmwire.vendor.mtk.hw.PCCIFPeripheral import PCCIF_Periph
from firmwire.vendor.mtk.ccif_irq_profile import bind_profile, validate_profile
from firmwire.vendor.mtk.loader import MTKLoader


def profile(cpus=2):
    return dict(schema="firmwire.ccif-normal-irq-analysis/v1", rom_sha256="a"*64,
        cpu_model="synthetic", evidence=["synthetic test"], controller="irq", transport="ccif",
        minimum_inclusive=True, outputs=[dict(cpu_index=i,pin=5) for i in range(cpus)],
        routes=[dict(channel_mask=3,source=6)])


class ChannelRoutingTests(unittest.TestCase):
    def test_deferred_line_is_only_driven_by_emulator_flush(self):
        import threading
        calls = []
        handoff = DeferredIRQOutputs([lambda level: calls.append((threading.get_ident(),level))])
        worker = threading.Thread(target=lambda: handoff.input(0)(True))
        worker.start()
        worker.join()
        self.assertEqual(calls,[])
        handoff.flush()
        self.assertEqual(calls,[(threading.get_ident(),True)])
        handoff.input(0)(False)
        handoff.flush()
        self.assertEqual(handoff.snapshot()["transitions"],[2])

    def test_multiple_masks_coalescing_and_ack_levels(self):
        lines = [[], []]
        router = ChannelIRQRouter([(3, lines[0].append), (12, lines[1].append)])
        doorbell = ChannelDoorbell(4, router)
        for channel in (0,1,0,2): doorbell.notify(channel)
        doorbell.acknowledge(1)
        self.assertEqual(lines, [[True],[True]])
        doorbell.acknowledge(6)
        self.assertEqual(lines, [[True,False],[True,False]])
        self.assertEqual(router.snapshot()["transitions"], [2,2])

    def test_invalid_routes_and_pending_are_rejected(self):
        for routes in ([], [(0, Mock())], [(True, Mock())], [(2**32, Mock())],
                       [(1,Mock()),(1,Mock())], [(1,None)]):
            with self.assertRaises(ValueError): ChannelIRQRouter(routes)
        router = ChannelIRQRouter([(1,Mock())])
        for pending in (-1,2**32,True):
            with self.assertRaises(ValueError): router(pending)

    def test_batch_configuration_has_no_intermediate_edges_or_partial_invalid_write(self):
        edges = []
        core = RoutedLevelIRQController(2,[edges.append])
        core.configure_many([(0,1,[0]),(1,2,[])])
        for source in (0,1):
            core.set_mask(source,False)
            core.set_level(source,True)
        self.assertEqual(edges,[True])
        core.configure_many([(0,1,[]),(1,2,[0])])
        self.assertEqual(edges,[True])
        before = core.snapshot()
        with self.assertRaises(ValueError): core.configure_many([(0,0,[0]),(1,0,[9])])
        self.assertEqual(core.snapshot(),before)


class DeliveryTests(unittest.TestCase):
    def test_unselected_nmi_group_bytes_are_preserved_not_interpreted(self):
        delivery = self.delivery()
        value = 0x04110101  # group 17 belongs to source 10, not selected source 6
        delivery.write(0x508,4,value)
        self.assertEqual(delivery.read(0x508,4),value)
        self.assertEqual(delivery.bank.groups[10],0)
        with self.assertRaises(NotImplementedError): delivery.write(0x504,4,value)
        self.assertEqual(delivery.read(0x504,4),0)

    def delivery(self, count=2):
        self.edges = [[] for _ in range(count)]
        return MdcirqLevelDelivery([edge.append for edge in self.edges], [6],
                                  minimum_inclusive=True, source_count=32)

    def test_idle_equality_mask_aliases_claim_return_and_level_reassert(self):
        delivery = self.delivery()
        with self.assertRaises(NotImplementedError): delivery.set_level(6,True)
        delivery.write(0x1a8,4,1)
        delivery.write(0x600,4,1)  # output 1, inverse mask
        delivery.set_level(6,True)
        self.assertEqual(self.edges,[[],[]])
        delivery.write(0x40,4,64)
        self.assertEqual(delivery.read(0x28,4),None)  # outside this source-count bank
        self.assertEqual(delivery.read(0x224,4),255)  # idle priority discriminator
        self.assertEqual(self.edges[1],[True])
        self.assertEqual(delivery.read(0xc24,4),6)
        self.assertEqual(delivery.read(0x224,4),127)
        self.assertEqual(self.edges[1],[True,False])
        delivery.write(0xc74,4,0xffff)
        self.assertEqual(self.edges[1],[True,False,True])
        delivery.set_level(6,False)
        self.assertEqual(delivery.snapshot()["claims"],[0,1])
        self.assertEqual(delivery.snapshot()["returns"],[0,1])

    def test_modes_refuse_selected_edge_broadcast_nmi_and_software_trigger(self):
        delivery = self.delivery()
        for offset in (0xa0,0xc0,0x100,0x160,0x80,0x120):
            before = delivery.snapshot()
            with self.subTest(offset=offset), self.assertRaises(NotImplementedError):
                delivery.write(offset,4,64)
            self.assertEqual(delivery.snapshot(),before)
        self.assertFalse(delivery.write(0xc0,4,128)) # different source is not modeled
        with self.assertRaises(NotImplementedError): delivery.write(0x1a8,4,0)
        for source in (1,True):
            with self.assertRaises(ValueError): delivery.set_level(source,True)

    def test_topology_and_unknown_registers_remain_explicit(self):
        for count in (1,2,4): self.assertEqual(len(self.delivery(count).claims),count)
        with self.assertRaises(ValueError): self.delivery(8) # legacy banks would overlap
        delivery = self.delivery()
        self.assertIsNone(delivery.read(0x800,4))
        self.assertFalse(delivery.write(0x800,4,123))
        self.assertEqual(delivery.snapshot()["other_registers"],"existing-passthrough")
        self.assertEqual(delivery.snapshot()["unselected_inputs"],"unmodeled")


class BindingTests(unittest.TestCase):
    def machine(self):
        controller, transport = object.__new__(MDCIRQ_Periph), object.__new__(PCCIF_Periph)
        transport.reply_doorbell = ChannelDoorbell(4)
        loader = SimpleNamespace(boot_mode="native",loader_args={"cpu_model":"synthetic"},
            capability_report={"rom_sha256":"a"*64,"engine_topology":{"realized_cpu_objects":2},
                "ccif_notifications":{"abi":"ring-index-channel-bit-analysis"}},write_capability_report=Mock())
        return SimpleNamespace(loader=loader,panda=SimpleNamespace(cb_after_block_exec=Mock()),
                               peripheral_map={"irq":controller,"ccif":transport})

    def test_binding_and_relocated_source_without_image_specific_code(self):
        machine = self.machine()
        endpoints = [Mock(),Mock()]
        factory = Mock(side_effect=endpoints)
        with patch("builtins.open",mock_open(read_data=json.dumps(profile()).encode())):
            bind_profile(machine,"profile.json",factory)
        controller = machine.peripheral_map["irq"].irq_delivery
        controller.write(0x1a8,4,1)
        controller.write(0x600,4,1)
        controller.write(0x40,4,64)
        machine.peripheral_map["ccif"].reply_doorbell.notify(1)
        factory.assert_called()
        endpoints[1].assert_not_called()
        machine.panda.cb_after_block_exec.call_args.args[0](None,None,None)
        endpoints[1].assert_called_once_with(True)
        self.assertFalse(machine.loader.capability_report["ccif_irq"]["boot_verified"])
        with patch("builtins.open",mock_open(read_data=json.dumps(profile()).encode())), self.assertRaises(ValueError):
            bind_profile(machine,"profile.json",factory)

    def test_profile_mismatch_unknown_fields_topology_and_duplicate_routes(self):
        original = profile()
        invalid = [dict(original,rom_sha256="b"*64),dict(original,cpu_model="other"),
                   dict(original,extra=True),dict(original,minimum_inclusive=1),dict(original,evidence=[]),
                   dict(original,outputs=[original["outputs"][0]]*2),dict(original,routes=original["routes"]*2),
                   dict(original,routes=[dict(channel_mask=1,source=256)]),
                   dict(original,outputs=[dict(cpu_index=0,pin=1),dict(cpu_index=1,pin=5)])]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError): validate_profile(item,"a"*64,"synthetic",2)
        for count in (1,2,4): validate_profile(profile(count),"a"*64,"synthetic",count)
        with self.assertRaises(ValueError): validate_profile(profile(8),"a"*64,"synthetic",8)

    def test_bad_binding_never_constructs_endpoints(self):
        for mismatch in ("type","notifications","native","topology","abi"):
            machine, factory = self.machine(), Mock()
            if mismatch=="type": machine.peripheral_map["irq"] = SimpleNamespace()
            if mismatch=="notifications": machine.peripheral_map["ccif"].reply_doorbell=None
            if mismatch=="native": machine.loader.boot_mode="rehosted"
            if mismatch=="topology": machine.loader.capability_report.pop("engine_topology")
            if mismatch=="abi": machine.loader.capability_report.pop("ccif_notifications")
            with patch("builtins.open",mock_open(read_data=json.dumps(profile()).encode())), self.assertRaises(ValueError):
                bind_profile(machine,"profile.json",factory)
            factory.assert_not_called()

    def test_loader_rejects_missing_prerequisites_before_mapping(self):
        for args, mode in (({},"native"),({"cpu_topology":"explicit"},"native"),
                ({"cpu_topology":"explicit","ccif_notifications":"ring-index-channel-bit-analysis"},"rehosted")):
            loader=object.__new__(MTKLoader)
            loader.loader_args=dict(args,ccif_irq_profile="explicit.json")
            loader.boot_mode=mode
            loader.build_peripheral_maps=Mock()
            with self.assertRaises(ValueError): loader.build_memory_map()
            loader.build_peripheral_maps.assert_not_called()
