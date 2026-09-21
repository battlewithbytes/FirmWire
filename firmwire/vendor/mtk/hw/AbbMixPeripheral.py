"""Halfword MMIO adapter for an explicitly selected software calibration model."""
import json

from firmwire.hw.peripheral import PassthroughPeripheral
from .abbmix import SelectedCalibrationAnalysis


class AbbMixAnalysisPeripheral(PassthroughPeripheral):
    def __init__(self, name, address, size, *, calibration, modeled_range=None, **kwargs):
        if not isinstance(calibration, SelectedCalibrationAnalysis):
            raise ValueError("Explicit calibration model required")
        if max(set(calibration.initial) | set(calibration.layout.status)) + 2 > size:
            raise ValueError("Calibration layout exceeds MMIO window")
        bounds = (0, size) if modeled_range is None else modeled_range
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or any(type(v) is not int or v % 2 for v in bounds)
                or not 0 <= bounds[0] < bounds[1] <= size
                or any(not bounds[0] <= a < bounds[1] for a in set(calibration.initial) | set(calibration.layout.status))):
            raise ValueError("Invalid modeled calibration subrange")
        super().__init__(name, address, size, **kwargs)
        self.calibration = calibration
        self.modeled_range = tuple(bounds)
        self.backing_accesses = {"read": 0, "write": 0}
        self.log.warning("SOFTWARE ABB CALIBRATION: synthetic result backend, no analog fidelity")

    def __getstate__(self):
        raise NotImplementedError("Calibration analysis snapshots are not supported; cold restart")

    def enable_control_observer(self):
        pass

    def control_observation(self):
        return dict(self.calibration.facts(), modeled_range=list(self.modeled_range),
                    outside_model_policy="preserved-legacy-zero-initialized-RAM-not-hardware",
                    backing_accesses=dict(self.backing_accesses))

    def _modeled(self, offset, size):
        if (type(offset) is not int or type(size) is not int or size not in (1, 2, 4, 8)
                or not 0 <= offset < offset + size <= self.size):
            raise ValueError("Invalid calibration page access")
        start, end = self.modeled_range
        intersects = offset < end and offset + size > start
        if intersects and not start <= offset < offset + size <= end:
            raise ValueError("Access crosses modeled calibration boundary")
        return intersects

    def hw_read(self, offset, size):
        try:
            if not self._modeled(offset, size):
                self.backing_accesses["read"] += 1
                return super().hw_read(offset, size)
            return self.calibration.read(offset, size)
        except (ValueError, NotImplementedError):
            self.log.error("ABB calibration unsupported: %s", json.dumps(self.control_observation()))
            raise

    def hw_write(self, offset, size, value):
        try:
            if not self._modeled(offset, size):
                if type(value) is not int or not 0 <= value < 1 << (8 * size):
                    raise ValueError("Invalid calibration page write")
                self.backing_accesses["write"] += 1
                return super().hw_write(offset, size, value)
            return self.calibration.write(offset, size, value)
        except (ValueError, NotImplementedError):
            self.log.error("ABB calibration unsupported: %s", json.dumps(self.control_observation()))
            raise
