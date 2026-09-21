"""Explicit 16-address/16-data PMIC write adapter; BSI reads remain unknown."""
from .pmic import PmicTarget, check_result, uint
from .rf_serial import SerialResult, SerialTarget


class PmicBsiWriteTarget(SerialTarget):
    def __init__(self, target):
        if not isinstance(target, PmicTarget):
            raise ValueError("BSI PMIC needs an explicit shared target")
        self.target = target
        self.reset()

    def reset(self):
        # A controller/transport reset must not reset another transport's device.
        self.writes = 0

    def exchange(self, command):
        if command.read or command.extended:
            return SerialResult("unresolved", reason="pmic-bsi-read-or-extended-unsupported")
        if (len(command.data) != 2 or any(not uint(v, 32) for v in command.data)
                or command.data[1] != 0):
            return SerialResult("unresolved", reason="pmic-bsi-word-format-unsupported")
        # RF immediate send does not set MIPI length registers. Do not decode
        # their stale contents as PMIC framing or select behavior by guest PC.
        word = command.data[0]
        reply = check_result(self.target.write(word >> 16, word & 0xffff), read=False)
        if reply.status == "unresolved":
            return SerialResult("unresolved", reason=reply.reason)
        self.writes += 1
        return SerialResult("write-complete", reason=reply.reason)

    def facts(self):
        return dict(kind="pmic-bsi16-write-analysis/v1", writes=self.writes,
                    bsi_reads_supported=False, silicon_verified=False,
                    target=self.target.facts())
