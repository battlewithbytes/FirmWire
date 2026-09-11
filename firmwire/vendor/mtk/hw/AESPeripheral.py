## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
import logging

from . import PassthroughPeripheral
from .aes_observation import AESControlTrace

# this is SEJ aka 'hacc' in the public linux kernel source
class AES_TOP0_Periph(PassthroughPeripheral):
    def __init__(self, name, address, size, **kwargs):
        super().__init__(name, address, size, **kwargs)
        self._control_trace = None
        self._analysis = None

    def enable_analysis(self, key_provider):
        from .sej_analysis import VirtualSEJ
        self._analysis = VirtualSEJ(key_provider)
        self.log.warning("SEJ: synthetic software-key analysis enabled; no vendor HUK or attestation")

    def analysis_facts(self):
        return self._analysis.facts() if self._analysis is not None else None

    def enable_control_observer(self):
        self._control_trace = AESControlTrace()

    def control_observation(self):
        return self._control_trace.snapshot() if self._control_trace is not None else None

    def hw_read(self, offset, size):
        value = self._read_unobserved(offset, size)
        if self._control_trace is not None:
            self._control_trace.record("read", offset, size, value)
        return value

    def _read_unobserved(self, offset, size):
        if self._analysis is not None:
            return self._analysis.read(offset, size)
        if offset == 0x8:
            self.log.debug("read AES_TOP0:8")

            if self.mem[0x8] == 1:
                return 0x8000
            elif self.mem[0x8] == 0x8:  # 0x40000008
                return 0x80000000
            elif self.mem[0x8] == 0x2:
                return 0x8
            return None
        else:
            return super().hw_read(offset, size)

    def hw_write(self, offset, size, value):
        if self._control_trace is not None:
            self._control_trace.record("write", offset, size, value)
        if self._analysis is not None:
            return self._analysis.write(offset, size, value)
        if offset == 0x0:
            self.log.debug("AES write CON")
        elif offset == 0x4:
            self.log.debug("AES write ACON")
        elif offset == 0x8:
            self.log.debug("AES write ACON2")
        elif offset == 0xC:
            self.log.debug("AES write ACONK")
        elif offset >= 0x10 and offset <= 0x1C:
            self.log.debug("AES write ASRC")
        elif offset >= 0x20 and offset <= 0x3C:
            self.log.debug("AES write AKEY")
        elif offset == 0x40 and offset <= 0x4C:
            self.log.debug("AES write ACFG")
        elif offset >= 0x50 and offset <= 0x5C:
            self.log.debug("AES write AOUT")
        elif offset >= 0x60 and offset <= 0x7C:
            self.log.debug("AES write SW_OTP")
        elif offset >= 0x80 and offset <= 0x88:
            self.log.debug("AES write SECINIT")
        return super().hw_write(offset, size, value)
