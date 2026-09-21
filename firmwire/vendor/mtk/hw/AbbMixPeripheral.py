"""Halfword MMIO adapter for an explicitly selected software calibration model."""
import json

from firmwire.hw.peripheral import FirmWirePeripheral
from .abbmix import SelectedCalibrationAnalysis


class AbbMixAnalysisPeripheral(FirmWirePeripheral):
    def __init__(self, name, address, size, *, calibration, **kwargs):
        if not isinstance(calibration, SelectedCalibrationAnalysis):
            raise ValueError("Explicit calibration model required")
        if max(set(calibration.initial) | set(calibration.layout.status)) + 2 > size:
            raise ValueError("Calibration layout exceeds MMIO window")
        super().__init__(name, address, size, **kwargs)
        self.calibration = calibration
        self.log.warning("SOFTWARE ABB CALIBRATION: synthetic result backend, no analog fidelity")

    def __getstate__(self):
        raise NotImplementedError("Calibration analysis snapshots are not supported; cold restart")

    def enable_control_observer(self):
        pass

    def control_observation(self):
        return self.calibration.facts()

    def hw_read(self, offset, size):
        try:
            return self.calibration.read(offset, size)
        except (ValueError, NotImplementedError):
            self.log.error("ABB calibration unsupported: %s", json.dumps(self.control_observation()))
            raise

    def hw_write(self, offset, size, value):
        try:
            return self.calibration.write(offset, size, value)
        except (ValueError, NotImplementedError):
            self.log.error("ABB calibration unsupported: %s", json.dumps(self.control_observation()))
            raise
