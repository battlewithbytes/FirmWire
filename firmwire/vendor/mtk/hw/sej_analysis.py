"""Bounded polled MTK SEJ model, independent of CPU/scheduler/firmware hooks.

Hardware-key binding is synthetic. No IRQ/DMA/TEE/attestation implementation.
Register words are little endian; byte-swap mode is unsupported.
"""


class VirtualSEJ:
    def __init__(self, key_provider):
        from Crypto.Cipher import AES
        self._aes = AES
        self._provider = key_provider
        self.operations = 0
        self.derivations = 0
        self.faults = 0
        self.reset()

    def reset(self):
        self._regs = bytearray(0x8c)
        self._chain = None
        self._bound_key = None
        self.last_fault = None

    def _word(self, offset):
        return int.from_bytes(self._regs[offset:offset + 4], "little")

    def _put(self, offset, value):
        self._regs[offset:offset + 4] = value.to_bytes(4, "little")

    def _fault(self, reason):
        self.faults += 1
        self.last_fault = reason
        self._put(8, 0)

    def _access(self, offset, size):
        if size != 4 or offset < 0 or offset > 0x88 or offset % 4:
            self._fault("unsupported register access")
            return False
        return True

    def read(self, offset, size):
        return self._word(offset) if self._access(offset, size) else 0

    def write(self, offset, size, value):
        if not self._access(offset, size):
            return False
        if not isinstance(value, int) or not 0 <= value <= 0xffffffff:
            self._fault("invalid register word")
            return False
        if 0x50 <= offset <= 0x5c:
            self._fault("output registers are read-only")
            return False
        if offset == 8:
            return self._command(value)
        if (offset == 0 and value != 0 or
                offset == 4 and (value & ~0x33 or value & 0x30 == 0x30) or
                offset == 0xc and value not in (0, 0x10, 0x110)):
            self._fault("unsupported control mode")
            return False
        self._put(offset, value)
        if 0x40 <= offset <= 0x4c:
            self._chain = None
        return True

    def _key_length(self):
        return {0: 16, 0x10: 24, 0x20: 32}[self._word(4) & 0x30]

    def _command(self, value):
        if value == 2:
            self._regs[0x10:0x20] = bytes(16)
            self._regs[0x50:0x60] = bytes(16)
            self._chain = None
            self.last_fault = None
            self._put(8, 8)
            return True
        if self.last_fault is not None:
            return False
        if value in (0x40000000, 0x40000008):
            self._bound_key = self._provider.derive(bytes(self._regs[0x60:0x80]), self._key_length())
            self.derivations += 1
            self._put(8, 0x80000000)
            return True
        if value == 0:
            self._put(8, 0)
            return True
        if value != 1:
            self._fault("unsupported command")
            return False
        length = self._key_length()
        key_control = self._word(0xc)
        if key_control & 0x100 and length != 16:
            self._fault("analysis feedback supports AES-128 only")
            return False
        if key_control & 0x10:
            if self._bound_key is None:
                self._bound_key = self._provider.derive(bytes(self._regs[0x60:0x80]), length)
                self.derivations += 1
            if len(self._bound_key) != length:
                self._fault("bound key length changed without initialization")
                return False
            key = self._bound_key
        else:
            key = bytes(self._regs[0x20:0x20 + length])
        control = self._word(4)
        source = bytes(self._regs[0x10:0x20])
        encrypt = bool(control & 1)
        cbc = bool(control & 2)
        iv = self._chain if self._chain is not None else bytes(self._regs[0x40:0x50])
        cipher = (self._aes.new(key, self._aes.MODE_CBC, iv=iv) if cbc else
                  self._aes.new(key, self._aes.MODE_ECB))
        output = cipher.encrypt(source) if encrypt else cipher.decrypt(source)
        self._regs[0x50:0x60] = output
        self._chain = (output if encrypt else source) if cbc else None
        if key_control & 0x100:
            self._bound_key = output  # Explicitly analysis-only feedback.
        self.operations += 1
        self._put(8, 0x8000)
        return True

    def facts(self):
        return dict(self._provider.facts(), operations=self.operations,
                    derivations=self.derivations, faults=self.faults,
                    last_fault=self.last_fault, mmio="polled AES ECB/CBC, little-endian words",
                    vendor_derivation_compatible=False)
