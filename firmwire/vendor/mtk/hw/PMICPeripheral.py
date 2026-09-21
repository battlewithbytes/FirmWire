## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
import logging

from . import PassthroughPeripheral
from .pmic import PmicTarget, PmicWacsControl

# see the Linux kernel driver (mtk-pmic-wrap)
# modern WACS (wrapper access?) regs in order:
#  CMD: write commands to this (i.e. a register address)
#  RDATA: this has combined state (high 16 bits) and data (low 16 bits)
#  VLDCLR: write 1 to clear valid
#  the users seem to do something like this:
#  - make sure INIT_DONE set
#  - wait for IDLE state
#  - set CMD reg to (is_write << 31) + ((address >> 1) << 16)
#  - wait for WFVLDCLR state
#  - lower 16 bits are data
#  - write to VLDCLR
class WACS:
    def __init__(self, log, wacsid, init_done_offset=21):
        self.log = log
        self.wacsid = wacsid
        self.state = 0
        self.init_done_offset = init_done_offset
        self.data = 0

    def read(self, offset):
        self.log.info(f"WACS{self.wacsid} read {offset}")
        if offset == 0x4:
            # data, WACS_INIT_DONE, FSM state
            return self.data | (1 << self.init_done_offset) | (self.state << 16)
        assert False

    def write(self, offset, value):
        self.log.info(f"WACS{self.wacsid} write {value:x} to {offset}")
        if offset == 0x0:
            address = ((value & 0xFFFF0000) >> 16) << 1
            is_write = value >> 31
            if self.wacsid == 0 and address == 0x8 and not is_write:
                # HWCID
                self.data = 1
            else:
                self.log.warning(
                    f"!!! WACS{self.wacsid} access (write={is_write}) to unknown {address:x}"
                )
                self.data = 0
            self.state = 6  # WFVLDCLR (wait for valid clear)
        elif offset == 0x8:
            # clear valid
            self.data = 0
            self.state = 0  # IDLE
        else:
            assert False


class PMIC_WRAP_Periph(PassthroughPeripheral):
    def __init__(self, name, address, size, pmic_target=None, **kwargs):
        if pmic_target is not None and not isinstance(pmic_target, PmicTarget):
            raise ValueError("Expected an explicit PMIC target")
        super().__init__(name, address, size, **kwargs)

        self.pmic_target = pmic_target
        self.wacs = []
        wacs_init_done_off = kwargs.get("wacs_init_done_offset", 21)
        for n in range(4):
            self.wacs.append(PmicWacsControl(pmic_target, init_done_offset=wacs_init_done_off)
                             if pmic_target is not None else WACS(self.log, n, wacs_init_done_off))
        if pmic_target is not None:
            self.log.warning("SHARED PMIC ANALYSIS: explicit target; unknown WACS requests remain pending")

    def _strict_access(self, offset, size):
        if getattr(self, "pmic_target", None) is not None and (type(size) is not int or size != 4
                or type(offset) is not int or offset % 4):
            raise ValueError("Strict WACS requires aligned 32-bit accesses")

    def hw_read(self, offset, size):
        if 0xC00 <= offset <= (0xC3f if getattr(self, "pmic_target", None) is not None else 0xC38):
            self._strict_access(offset, size)
            offset = offset - 0xC00
            return self.wacs[offset // 0x10].read(offset % 0x10)
        return super().hw_read(offset, size)

    def hw_write(self, offset, size, value):
        if 0xC00 <= offset <= (0xC3f if getattr(self, "pmic_target", None) is not None else 0xC38):
            self._strict_access(offset, size)
            offset = offset - 0xC00
            self.wacs[offset // 0x10].write(offset % 0x10, value)
            return True
        return super().hw_write(offset, size, value)

    def enable_control_observer(self):
        pass

    def control_observation(self):
        if getattr(self, "pmic_target", None) is None:
            return {"kind": "legacy-pmic-wrapper", "unknown_reads_return_zero": True,
                    "silicon_verified": False}
        return {"kind": "shared-pmic-wrapper-analysis/v1", "target": self.pmic_target.facts(),
                "channels": [channel.facts() for channel in self.wacs]}

    def pre_snapshot_handler(self, snapshot_name):
        if getattr(self, "pmic_target", None) is not None:
            raise RuntimeError("Shared PMIC snapshots require identity-preserving machine serialization")
