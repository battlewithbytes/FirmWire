"""Explicit ROM-bound wiring for a selected normal-IRQ analysis experiment."""
import hashlib
import json

from firmwire.hw.irq_router import ChannelIRQRouter
from firmwire.emulator.mips_irq import MipsIRQInput, DeferredIRQOutputs
from .hw.MDCPeripheral import MDCIRQ_Periph
from .hw.PCCIFPeripheral import PCCIF_Periph
from .hw.mdcirq_delivery import MdcirqLevelDelivery


def validate_profile(profile, rom_sha256, cpu_model, cpu_count):
    fields = {"schema", "rom_sha256", "cpu_model", "evidence", "controller", "transport",
              "minimum_inclusive", "outputs", "routes"}
    if isinstance(profile, dict) and profile.get("schema") == "firmwire.ccif-normal-irq-analysis/v2":
        fields.add("software_sources")
    if (not isinstance(profile, dict) or set(profile) != fields
            or profile["schema"] not in ("firmwire.ccif-normal-irq-analysis/v1", "firmwire.ccif-normal-irq-analysis/v2")
            or profile["rom_sha256"] != rom_sha256 or profile["cpu_model"] != cpu_model):
        raise ValueError("IRQ profile identity/schema does not match")
    if type(profile["minimum_inclusive"]) is not bool:
        raise ValueError("explicit idle priority comparison policy required")
    if (not isinstance(profile["evidence"], list) or not profile["evidence"]
            or any(not isinstance(item, str) or not item for item in profile["evidence"])):
        raise ValueError("IRQ profile requires provenance")
    for name in ("controller", "transport"):
        if not isinstance(profile[name], str) or not profile[name]: raise ValueError("explicit device names required")
    outputs = profile["outputs"]
    if type(cpu_count) is not int or cpu_count < 1 or not isinstance(outputs, list) or len(outputs) != cpu_count:
        raise ValueError("IRQ outputs must match realized CPU topology")
    cpus = []
    for row in outputs:
        if (not isinstance(row, dict) or set(row) != {"cpu_index", "pin"}
                or type(row["cpu_index"]) is not int or not 0 <= row["cpu_index"] < cpu_count
                or type(row["pin"]) is not int or not 2 <= row["pin"] <= 7):
            raise ValueError("invalid CPU IRQ endpoint")
        cpus.append(row["cpu_index"])
    if len(set(cpus)) != cpu_count: raise ValueError("duplicate CPU output")
    routes = profile["routes"]
    if not isinstance(routes, list) or not 1 <= len(routes) <= 32: raise ValueError("explicit channel routes required")
    masks, sources = [], []
    for row in routes:
        if (not isinstance(row, dict) or set(row) != {"channel_mask", "source"}
                or type(row["source"]) is not int or not 0 <= row["source"] < 256):
            raise ValueError("invalid source route")
        masks.append((row["channel_mask"], lambda _: None))
        sources.append(row["source"])
    ChannelIRQRouter(masks)
    software = profile.get("software_sources", [])
    if (not isinstance(software, list) or len(software) > 32
            or ("software_sources" in fields and not software)):
        raise ValueError("requires bounded explicit software sources")
    # Exercise all geometry checks without touching a native CPU or device.
    MdcirqLevelDelivery([lambda _: None] * cpu_count, sources,
                        minimum_inclusive=profile["minimum_inclusive"], software_sources=software)
    return profile


def bind_profile(machine, path, endpoint_factory=MipsIRQInput):
    loader = machine.loader
    if loader.boot_mode != "native": raise ValueError("IRQ binding requires native mode")
    with open(path, "rb") as stream: raw = stream.read(65537)
    if len(raw) > 65536: raise ValueError("IRQ profile exceeds 64 KiB")
    facts = loader.capability_report
    if facts.get("ccif_notifications", {}).get("abi") != "ring-index-channel-bit-analysis":
        raise ValueError("IRQ binding requires realized ring notification ABI")
    profile = validate_profile(json.loads(raw), facts["rom_sha256"], loader.loader_args["cpu_model"],
        facts.get("engine_topology", {}).get("realized_cpu_objects"))
    controller = machine.peripheral_map.get(profile["controller"])
    transport = machine.peripheral_map.get(profile["transport"])
    if (not isinstance(controller, MDCIRQ_Periph) or not isinstance(transport, PCCIF_Periph)
            or getattr(transport, "reply_doorbell", None) is None):
        raise ValueError("IRQ binding requires typed controller and enabled ring notifications")
    if (hasattr(controller, "irq_delivery") or hasattr(transport, "reply_irq_router")
            or transport.reply_doorbell.on_change is not None or transport.reply_doorbell.pending):
        raise ValueError("IRQ wiring must be exclusive and installed before execution")
    if any(row["channel_mask"] >= 1 << transport.reply_doorbell.channels for row in profile["routes"]):
        raise ValueError("IRQ route exceeds notification channel count")
    outputs = [endpoint_factory(machine.panda, **row) for row in profile["outputs"]]
    deferred = DeferredIRQOutputs(outputs)
    controller.enable_irq_delivery([deferred.input(index) for index in range(len(outputs))],
                                   [row["source"] for row in profile["routes"]],
                                   minimum_inclusive=profile["minimum_inclusive"],
                                   software_sources=profile.get("software_sources", []))
    @machine.panda.cb_after_block_exec
    def flush_irq_outputs(cpu, tb, exit_code):
        deferred.flush()
    controller.irq_output_handoff = deferred
    router = ChannelIRQRouter([(row["channel_mask"],
        lambda level, source=row["source"]: controller.irq_delivery.set_level(source, level))
        for row in profile["routes"]])
    transport.reply_doorbell.on_change = router
    transport.reply_irq_router = router
    facts["ccif_notifications"]["cpu_interrupt_connected"] = True
    facts["ccif_irq"] = dict(schema=profile["schema"], profile_sha256=hashlib.sha256(raw).hexdigest(),
        output_handoff="emulator-thread-after-block",
        analysis_only=True, cpu_interrupt_connected=True, interrupt_delivery_verified=False,
        application_delivery_verified=False, boot_verified=False, profile=profile)
    loader.write_capability_report()
