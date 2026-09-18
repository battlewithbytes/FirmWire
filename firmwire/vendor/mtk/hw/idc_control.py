"""Reviewed IDC TX-counter enable subset, with an idle/absent scheduler.

Current ROM writes 1 to +0x0c after UART/ISR initialization. Sibling MT6768
drv_idc_init calls this 'Enable TX Count'; its consumer reads +0x10 low 16 bits.
No event scheduling or transmitted events exist here. Never manufacture their
completion, infer count units, or accept other scheduler/control operations.
"""
from firmwire.hw.peripheral import FirmWirePeripheral


class IdleIDCCounter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.enabled = False
        self.enable_writes = 0
        self.count_reads = 0
        self.last_unsupported = None

    def _access(self, offset, size):
        if type(offset) is not int or type(size) is not int or size != 4 or offset < 0 or offset % 4:
            raise ValueError("IDC counter requires aligned 32-bit access")

    def read(self, offset, size):
        self._access(offset, size)
        if offset == 0x10:
            self.count_reads += 1
            return 0  # no scheduler/backend transmission has occurred
        self.last_unsupported = {"offset": offset, "size": size, "direction": "read"}
        raise NotImplementedError("unreviewed IDC control read at %#x" % offset)

    def write(self, offset, size, value):
        self._access(offset, size)
        if type(value) is not int or not 0 <= value <= 0xffffffff:
            raise ValueError("IDC counter value must be an unsigned word")
        if offset == 0xc and value == 1:
            self.enabled = True
            self.enable_writes += 1
            return True
        self.last_unsupported = {"offset": offset, "size": size, "direction": "write"}
        raise NotImplementedError("unreviewed IDC control write at %#x" % offset)

    def facts(self):
        return dict(kind="mt6768-idc-idle-tx-counter/v1",enabled=self.enabled,
                    enable_writes=self.enable_writes,count_reads=self.count_reads,
                    transmit_count=0,scheduler_supported=False,peer_connected=False,
                    completion_fabricated=False,
                    last_unsupported=None if self.last_unsupported is None else dict(self.last_unsupported))


class MTKIDCControlPeripheral(FirmWirePeripheral):
    def __init__(self, name, address, size, **kwargs):
        super().__init__(name,address,size,**kwargs)
        self.control = IdleIDCCounter()

    def hw_read(self, offset, size):
        return self.control.read(offset,size)

    def hw_write(self, offset, size, value):
        return self.control.write(offset,size,value)

    def enable_control_observer(self):
        pass

    def control_observation(self):
        return self.control.facts()
