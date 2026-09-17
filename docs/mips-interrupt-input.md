# Native MIPS interrupt endpoint: verified primitive, not modem IRQ routing

`firmwire/emulator/mips_irq.py:MipsIRQInput` binds to the separately built PANDA
`configurable_mips_set_irq(cpu_index, pin, level)` function. The compiled function
resolves a real CPU, validates hardware pin 2..7 and boolean level, and uses
`qemu_set_irq` on its native input. This lets QEMU maintain Cause/IP and its CPU
interrupt request. No stale Python CPU layout, PC patch, fabricated interrupt
vector, forced IE bit or artificial ISR return is involved.

The CPU index is supplied by the platform's topology mapping, not inferred from
callback order, a modem hash, or a fixed two-VPE assumption. Missing engine symbols,
out-of-range values and nonexistent CPUs fail explicitly. The input owns no
interrupt controller policy. Pin 7 can be shared with a CPU timer on a board:
the platform must establish ownership; never independently drive overlapping
sources with this API. One controller must aggregate each shared pin upstream.

Threading: call from the emulator thread after CPU initialization. FirmWire's
forwarded MMIO can execute on an Avatar worker thread, so connecting its UART
callback directly to this function is **not** a supported production integration.
The eventual controller adapter needs an emulator-thread delivery mechanism that
also wakes halted CPUs. The synthetic native tests run on PANDA callbacks and do
not have this cross-thread problem.

## What is tested

`FIRMWIRE_TEST_NATIVE_IRQ=1 pytest tests/test_mips_irq_native.py` on the isolated
`firmwire-cockpit:irq` image runs synthetic guest code on both standard `24Kc` and
`cockpit-mtk-legacy`, with each board's physical mapping explicit. Backend RX
asserts UART IRQ while the guest has IE clear. The guest sees the pending bit,
enables its mask/IE, enters its own ISR, reads RX (deasserting the line), executes
ERET and records resumed execution. The test checks Cause/IP, architectural
interrupt ExcCode, received byte, line transitions and invalid native API inputs.
No modem image is involved and no guest interrupt handler is emulated in Python.

Python tests cover explicit CPU indices 0/1/3/7, strict type/range validation,
missing API and native rejection. Multi-VPE routing, priorities, nested IRQs,
guest WAIT wakeup and real firmware ISR execution are **not yet verified**.

## Why it is not attached to Lagos yet

The older MOLY source has IDC UART IRQ number 83 on MT6768, versus 149 on ELBRUS.
These are peripheral IDs, not CPU pins. Source `irqid_MT6768.h` names
`VPE_IRQID_SI_INT` as 5, but current-ROM evidence must confirm that route before
enabling it. `drv_mdcirq_reg.h` selects `__MDCIRQ_GCR_SIGNAL_DISABLE__` for this
source's MT6768 family: the ISR reads the APB MDCIRQ per-VPE ID registers and
returns the ID through its acknowledgement interface, rather than the alternate
GCR bank. Do not drop in QEMU's standard MIPS GIC at an assumed address.

Current FirmWire `MDCIRQ_Periph` mostly stores writes; mask/set/clear, priority,
group-to-VPE dispatch and acknowledgement are not implemented. Its GCR view is
plain memory. Connecting UART straight to CPU pin 5 would bypass those semantics
and cannot be called a supported modem interrupt implementation.

Next: cross-check current-ROM interrupt setup/ISR against the sibling source,
then implement a family-selected MDCIRQ controller shared by its register views.
Its source count, groups, priorities and CPU topology must be explicit parameters;
devices provide source levels, never a chosen PC or CPU. Test masked/unmasked RX,
level deassertion, competing sources, routing to each supported VPE, acknowledge/
return, and idle CPU wakeup before using it in a modem probe. This is a hardware
contract task, not an invitation to add generic mtkloader parsers.
