# Reusable UART and IDC control subset

`firmwire/hw/uart.py` is vendor-neutral. It contains no modem identity, address,
firmware PC, calibration dependency, or automatic peer response.

## Layers

- `UARTCore(fifo_depth, irq_sink)` owns bounded RX/TX queues, FIFO enable/reset,
  receive overrun, interrupt priority and level transitions. `receive(bytes)`
  supplies actual backend input; `drain_tx(count)` removes transmitted bytes.
  Call these on the emulator thread. An optional `irq_sink(bool)` receives level
  changes; the platform is responsible for routing that signal to the guest.
- `UARTRegisterBank(core, stride, access_sizes, fifo_triggers)` implements a
  limited 8250-style register frontend: DLAB DLL/DLM aliases, IER/IIR/FCR,
  LCR/MCR, LSR/MSR and scratch. This is not a complete 16550 implementation.
- `UARTPeripheral` in `hw/uart_peripheral.py` is the vendor-neutral FirmWire MMIO
  shell. It accepts a register bank; no UART logic is duplicated in adapters.
  FIFO overflow increments a counter and drops the new TX byte without turning
  an assigned UART write into an unassigned bus transaction.
- `MTKIDCUARTRegisters` in `vendor/mtk/hw/idc_uart.py` specializes four-byte
  spacing, 32-byte FIFOs, explicit RX threshold and baud configuration latches.
  `MTKIDCUARTPeripheral` connects it to the existing FirmWire MMIO framework;
  the constructor receives the address, not the UART implementation.
- The loader's explicit `idc_uart=mt6768-control` selection supplies the reviewed
  physical address `0xa60b0000`. It is disabled by default, independent of BSI/RF
  options, native-only, and never selected by ROM hash or instruction address.
  The existing MTK PANDA address translation handles the buffered alias. Do not
  add a second, independently stateful UART at the alias.

Different vendors should compose a core and their own register frontend. Matching
8250-like ABIs can reuse the register bank. A PL011-like ABI should not inherit
8250 register semantics just because both devices are UARTs. Existing Shannon
console stubs are unchanged; converting those needs separate ABI verification.

## Evidence and limits

The current Lagos ROM's baud setup matches the older MOLY MT6768 `drv_idc.c`
`drv_idc_set_baudrate` register sequence. `idc_reg.h` supplies the register names;
the sibling source is reference evidence, not universal MediaTek support.
No proprietary source is embedded here.

The opt-in mapping reports `control_only=true`, `peer_connected=false`,
`guest_irq_routed=false`, and `timing_verified=false` in capabilities. This
iteration deliberately has no guest interrupt connection or serial backend.
Queues only change on explicit operations: reading status does not drain TX or
invent RX. Therefore transmitting without a backend can fill TX and stall; this
must not be presented as a working connectivity peer.

Unsupported: receive timeout, parity/framing errors, modem/flow-control signals,
loopback, DMA, baud-accurate timing, IDC pattern matching/PM registers, and the
separate IDC control block. Unknown registers and unsupported control bits fail
explicitly. Baud registers are storage, not a simulated serial clock. Core reset
defaults are model defaults, not asserted silicon measurements.

IDC is a connectivity/coexistence link, not AP CCCI and not LTE packet injection.
No firmware instruction or runtime table is patched by the UART model.

## Verification

`tests/test_uart.py` varies stride (1/2/4), access width, FIFO depth
(1/2/16/32/64), DLAB aliases, overflow/reset, interrupt priority/acknowledgement,
FIFO threshold, instance isolation, serialization, and loader opt-in/rejection.
The IDC baud sequence is transcribed independently in tests. Unknown offsets
remain failures. No modem fixture is needed.

`FIRMWIRE_TEST_NATIVE_UART=1 pytest tests/test_uart_native.py` executes synthetic
MIPS instructions under the development PANDA against two independently placed
real peripheral adapters. It checks guest readback, no divisor writes becoming
TX bytes, explicit TX capture, empty RX, and independent register state.

The Cockpit probe accepts `DRDI_IDC_UART=mt6768-control`; other existing analysis
settings still need explicit selection. A passing UART test does not establish
modem task startup, peer communication, AP handshake or packet processing.
