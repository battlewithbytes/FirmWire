"""Relocatable adapter for explicit digital BSI observation/pending modes."""
from firmwire.hw.peripheral import PassthroughPeripheral
from .bsi import BsiImmediateControl, ReadCompletionLayout, SerialCommand
from .rf_serial import SerialBus, WriteCaptureTarget, SoftwareMt6177Target, validate_software_rf_profile
from .hwpor import HwporLayout, HwporSequencer


class BSIImmediatePeripheral(PassthroughPeripheral):
    def __init__(self, name, address, size, bsi_mode="observe", rf_profile=None, **kwargs):
        super().__init__(name, address, size, **kwargs)
        capture = bsi_mode == "capture-writes"
        software = bsi_mode == "software-rf"
        if software != (rf_profile is not None):
            raise ValueError("Software RF requires an explicit profile and matching opt-in mode")
        self.rf_profile = None
        bus = SerialBus({0: WriteCaptureTarget(), 2: WriteCaptureTarget()}) if capture else None
        if software:
            self.rf_profile = validate_software_rf_profile(rf_profile,
                self.machine.loader.capability_report["rom_sha256"])
            bus = SerialBus({int(port): SoftwareMt6177Target(**identity)
                             for port, identity in self.rf_profile["ports"].items()})
        self.control = BsiImmediateControl(size=size, mode="pending" if capture or software else bsi_mode,
            read_layout=ReadCompletionLayout(0x1204, 0x1200, (0, 2)),
            serial_bus=bus)
        self.hwpor = HwporSequencer(HwporLayout(0x4000, 0x8000), self._hwpor_submit) if software else None
        if software:
            self.log.warning("SOFTWARE RF ANALYSIS %s: assumed identity/ECO %s; no silicon/calibration fidelity",
                             self.rf_profile["name"], self.rf_profile["ports"])
        self.log.warning("BSI %s analysis: RF/DSP not emulated; write capture=%s; no generated read data",
                         bsi_mode, capture)

    def hw_read(self, offset, size):
        if self._hwpor_access(offset):
            return self.hwpor.read(offset, size)
        return self.control.read(offset, size)

    def hw_write(self, offset, size, value):
        if self._hwpor_access(offset):
            self.hwpor.write(offset, value, size)
            return True
        return self.control.write(offset, size, value)

    def _hwpor_access(self, offset):
        return self.hwpor is not None and (0x4000 <= offset < 0x4090 or 0x8000 <= offset < 0x8400)

    def _hwpor_submit(self, write):
        command = SerialCommand(write.sequence, -1, write.port, False, False, (write.word, 0), (31, 0))
        result = self.control.serial_bus.exchange(command)
        if result.status == "unresolved" and result.value is None:
            return False
        if result.status != "write-complete" or result.value is not None:
            raise ValueError("HWPOR backend must complete the actual write")
        return True

    def advance_guest_blocks(self, blocks):
        # Explicit analysis clock: one logical tick per completed guest block,
        # delivered in 1024-block batches. No wall-clock or poll-based progress.
        if self.hwpor is not None:
            self.hwpor.advance(blocks)

    def enable_control_observer(self):
        pass  # Already bounded to 64 events; never changes peripheral behavior.

    def control_observation(self):
        facts = self.control.facts()
        if self.hwpor is not None:
            facts.update(software_rf_profile=self.rf_profile, hwpor=self.hwpor.facts(),
                         clock_policy="one-logical-tick-per-guest-block-batched-1024")
        return facts
