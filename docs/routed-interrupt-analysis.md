# Routed interrupt building blocks

`firmwire.hw.routed_irq.RoutedLevelIRQController` is an independent, opt-in
analysis primitive. It is **not installed into modem machines** and is not an
MDCIRQ register implementation. Constructors accept explicit source counts,
output callbacks and priority counts; tests cover 1, 2, 4 and 8 outputs. Nothing
chooses CPU topology or translates a firmware address.

The contract is unicast level interrupts: initial masking, explicit routing,
smaller-numbered priority first, an exclusive output threshold, nested claims,
LIFO completion, and reassertion if the device level remains high after EOI.
Clearing a device level does not complete its active interrupt. One source has
at most one owner, including while routes are reconfigured. Arbitration chooses
the lowest eligible output, explicitly an analysis policy rather than silicon
fairness. Broadcast, edge latching, software-trigger registers and NMI are not
implemented. Platform callbacks must run on the emulator thread and not reenter.

The existing `MipsIRQInput` adapter supplies native CPU endpoints. The native
test connects UART -> routed controller -> CPU input, with synthetic claim/EOI
registers serviced by the test harness. Both 24Kc and cockpit-mtk-legacy execute
an actual ISR, claim source 3, read UART RX, complete, and ERET. These synthetic
registers do **not** establish the MediaTek ABI or modem boot.

## Passive bus evidence

`firmwire.hw.register_observer.RegisterAccessObserver` records accesses already
performed by a device. It reads/writes no device memory itself and assigns no
semantics. It retains at most 1024 distinct (offset,width) pairs, first/last 16
events, read/write counts and last values/sequences. Dropped or rejected evidence
is counted. Snapshots are detached. Enabling the observer twice preserves history.

MDCIRQ's existing passthrough device can enable this observer through the
ROM-bound `observe_ram.peripheral_controls` configuration. Selection requires
the actual device class, not a hardcoded platform name. Default behavior remains
passthrough storage, including its known lack of pending/mask/claim semantics.
Do not interpret stored W1C/W1S values as effective controller state.

## Remaining MediaTek adapter boundary

Cockpit's target evidence confirms normal-page setup and an IRQ-ID read at
`+0xc20 + 4*VPE`, current priority at `+0x220 + 4*VPE`, and a return write at
`+0xc70 + 4*VPE`. Critically, the return value is the **previous** IRQ ID saved
by the dispatcher, not the currently claimed source. An adapter must translate
that restore contract; it cannot forward the value to `complete()` unchanged.

The remaining adapter must verify source IDs, installed handlers, CPU input,
group-mask polarity, priority/state thresholds and CCIF channel acknowledgement.
Do not enable a raw CPU interrupt just because a host reply was queued. In
particular, old-source CCIF source numbers remain unverified for the target.
