"""93xx MML2 MCU MMU *control-only* analysis model.

Evidence: mml2_mmu_93arch.obj dpcopro_mmu_clear_all_tlb writes 0x8000c000
to offset 0x40 and waits for bit 31 to clear. No translation/DMA backend is
attached, hence its hardware translation cache is empty. Completing this
operation says nothing about CPU TLBs, address translation or packet processing.
Unknown commands and writes to unimplemented registers fail explicitly.
"""


class MML2MMU93Control:
    SIZE = 0x1000
    COMMAND = 0x40
    INVALIDATE_ALL = 0x8000C000
    CONFIG = frozenset((0, 4, 0x100, 0x340))
    # Four pairs of read-only cached tags/page descriptors, and validity bits.
    CACHE = frozenset((0x200, 0x204, 0x210, 0x214, 0x220, 0x224,
                       0x230, 0x234, 0x2C0))

    def __init__(self):
        self.reset()

    def reset(self):
        self.registers = {offset: 0 for offset in self.CONFIG}
        self.command = 0
        self.invalidations = 0
        self.last_error = None

    def _access(self, offset, size):
        if (type(offset) is not int or type(size) is not int or
                size not in (1, 2, 4) or offset < 0 or
                offset + size > self.SIZE or offset % size or
                offset // 4 != (offset + size - 1) // 4):
            raise ValueError("MML2 MMU requires aligned in-bank 1/2/4-byte access")
        return offset & ~3, (offset & 3) * 8, (1 << (size * 8)) - 1

    def _unsupported(self, description):
        self.last_error = description
        raise NotImplementedError("93xx MML2 control-only model: " + description)

    def read(self, offset, size):
        word, shift, mask = self._access(offset, size)
        if word == self.COMMAND:
            value = self.command
        elif word in self.CONFIG:
            value = self.registers[word]
        elif word in self.CACHE:
            value = 0  # Empty cache: no translation backend or DMA requests.
        else:
            self._unsupported("unimplemented register read at %#x" % offset)
        return (value >> shift) & mask

    def write(self, offset, size, value):
        word, shift, mask = self._access(offset, size)
        if type(value) is not int or not 0 <= value <= mask:
            raise ValueError("MML2 MMU write value does not fit access width")
        if word == self.COMMAND:
            if offset != self.COMMAND or size != 4:
                self._unsupported("partial invalidation command write")
            if value != self.INVALIDATE_ALL:
                self._unsupported("unimplemented invalidation command %#x" % value)
            # Synchronous completion is valid for this empty control-only cache;
            # no fabricated timer, polling threshold or firmware-PC hook.
            self.command = value & ~0x80000000
            self.invalidations += 1
        elif word in self.CONFIG:
            self.registers[word] = (self.registers[word] & ~(mask << shift)) | (value << shift)
        else:
            self._unsupported("unimplemented/read-only register write at %#x" % offset)
        return True

    def translate(self, address):
        self._unsupported("translation/DMA backend is absent")

    def facts(self):
        return {"abi": "mtk-mml2-93xx-control-v1", "analysis_only": True,
                "invalidate_all_completed": self.invalidations,
                "command": self.command, "cache_entries": 0,
                "translation_supported": False, "dma_supported": False,
                "timing_model": "synchronous-empty-cache",
                "reset_model": "zero-analysis-state", "last_error": self.last_error}
