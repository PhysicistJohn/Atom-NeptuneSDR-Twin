# NeptuneSDR QEMU co-simulation layer

This directory contains the board-specific devices needed to run the public
P210 Linux image against QEMU 10.0.2.  It is not a second abstract board
model: the guest reaches the same physical addresses, SPI command format and
interrupt numbers described by the P210 device tree.

The direct QEMU path is the shortest path to useful pre-arrival firmware work.
This directory owns the virtual hardware; ARM source, firmware inputs, runtime
composition, and the canonical board-side ABI live in the separately pinned
[`Atom-NeptuneSDR-Firmware`](https://github.com/PhysicistJohn/Atom-NeptuneSDR-Firmware)
repository:

1. `p210-ad9361` replaces the NOR flash incorrectly attached to PS SPI0 chip
   select 0 by stock `xilinx-zynq-a9`.
2. `p210-sdr` supplies the AXI AD9361 RX/DDS register windows and both ADI AXI
   DMACs in the programmable-logic address range.
3. The unmodified ADI drivers perform their real identity, calibration, clock
   lock, PN timing-tune, DMA capability and IRQ handshakes against these
   devices.
4. `p210-fft` provides the proposed firmware-visible PL FFT contract at
   `0x7c450000`/GIC SPI 58 and executes a deterministic integer radix-2 FFT.
   It is a functional QEMU implementation, not evidence of synthesized or
   timing-closed XC7Z020 RTL.

RX buffer traffic is deliberately observable rather than zero-filled.  The
DMAC emits signed 12-bit samples in 16-bit little-endian IIO storage slots,
packed in the order used by the ADI driver and HDL: RX1 I, RX1 Q, RX2 I, RX2
Q.  Disabled scan channels are omitted in ascending scan-index order, just as
the ADI channel packer does.  Phase advances continuously across DMA segments
and cyclic callbacks, so buffer boundaries do not introduce phase resets.

The two default complex tones are exact bins of a 64-sample integer NCO:

| IIO pair | Frequency | Peak ADC code | Initial phase |
| --- | ---: | ---: | ---: |
| RX1 I/Q | `5/64 Fs` | 1536 | 0 degrees |
| RX2 I/Q | `13/64 Fs` | 1024 | 45 degrees |

For example, at 50 MSPS these appear at 3.90625 MHz and 10.15625 MHz.  A
capture length divisible by 64 places both tones exactly on FFT bins.  The
following QEMU globals override the defaults; step and phase are modulo 64,
and amplitudes above the signed 12-bit maximum clamp to 2047:

```text
-global p210-sdr.rx-tone0-step=5
-global p210-sdr.rx-tone1-step=13
-global p210-sdr.rx-tone0-amplitude=1536
-global p210-sdr.rx-tone1-amplitude=1024
-global p210-sdr.rx-tone0-phase=0
-global p210-sdr.rx-tone1-phase=8
```

The AXI-DMAC `X_LENGTH` register has the real 24-bit synthesis width rather
than acting like an unconstrained C `uint32_t`.  This detail is required by
the 4.14 ADI driver: it probes the width with an all-ones write and derives a
16 MiB maximum segment.  Returning all 32 bits would overflow that driver's
old `DIV_ROUND_UP()` expression and cause a kernel divide-by-zero when an IIO
buffer is enabled.

The pinned XSA handoff is also authoritative for optional capabilities. Both
DMAC instances have `DMA_2D_TRANSFER=false`, a 29-bit AXI memory address and
64-bit memory beats; RX has `CYCLIC=false`, while TX has `CYCLIC=true`.
Consequently `Y_LENGTH` and both stride registers read zero, only the
direction's memory-side address register is implemented, and addresses are
masked to `0x1ffffff8`.  RX/TX `FLAGS` reset to `0x2`/`0x3` respectively.
`STATUS` is reserved and remains zero because the handoff disables the
diagnostics interface. A failed QEMU address-space transaction therefore
retires with the core's normal `TRANSFER_DONE`/EOT behavior and is additionally
reported in QEMU's guest-error log; the model does not invent a guest-visible
status bit.

The DMAC also implements the core's four-entry descriptor queue and four
two-bit transfer IDs.  Configuration registers are snapshotted when a submit
is accepted; `TRANSFER_DONE[3:0]` is cleared on re-use and set on FIFO-ordered
completion.  This is observable with IIOD, which queues four receive buffers
before waiting.  A single-slot approximation loses the first three buffers
and makes host reads time out even though the last DMA write reached memory.

## Firmware/host capture proof

The integrated QEMU device was booted with the P210 Linux 4.14 kernel and the
Pluto v0.39 root filesystem/IIOD.  The pinned host libiio command

```text
iio_readdev -b 4096 -s 4096 cf-ad9361-lpc
```

returned 32,768 bytes (4,096 frames × four 16-bit scan channels), with no
kernel division-by-zero, oops or panic.  A direct DFT of that capture found
RX1 at bin 320/4096 (`5/64 Fs`, magnitude 1535.47) and RX2 at bin 832/4096
(`13/64 Fs`, magnitude 1023.49), both with zero DC.  A second capture using
4,095-frame DMA buffers crossed a non-period-aligned boundary without phase
reset.

The AD9361 model does not fake calibration by only clearing `0x016`.  RX
baseband-filter calibration also produces the R2346 and C3 component codes at
`0x1e6`, `0x1eb` and `0x1ec`.  The pinned driver consumes those values in
`ad9361_rx_adc_setup()` and would otherwise divide by zero.  For the P210
startup profile (40 MHz reference, BBPLL integer word 24, fractional word
`0x125c29` over the AD9361 modulus 2,088,960, tune divide 9), the model returns
`R2346=1`, `C3_MSB=0`, `C3_LSB=0x36`; this lies inside the `0x35..0x3b`
range visible in ADI traces from physical AD9361 devices.  Other requested
bandwidths derive a finite component tuple from the same inverse-RC equation
used by the driver.

The QEMU machine integration patch and build are maintained separately under
`qemu/` by the Twin runtime layer. The reusable device sources live under
`qemu-10.0.2/`; copy them into the corresponding QEMU source directories and
register them from the QEMU Meson files.

## Fidelity boundary

This layer is register-, firmware- and memory-DMA-visible.  RX DMA writes the
deterministic two-tone IQ source described above and TX DMA reads and discards
guest IQ bytes.  It therefore validates firmware, IIO/IIOD, scan packing,
buffer ownership, timing, interrupts and host interfaces, but it does **not**
claim RF waveform,
converter-noise, analog-filter, mixer, antenna or clock-jitter equivalence.
Those sample sources belong behind the DMA contact and can later be connected
to the existing Python model or an RTL/SystemC Remote-Port process.
Calibration timing is intentionally collapsed to immediate completion, and
the returned analog component codes are functional representatives rather
than a temperature/process model.

Remote-Port remains the higher-fidelity expansion seam, not the first boot
dependency.  AMD's Xilinx QEMU fork contains Remote-Port today; the upstream
QEMU series was only proposed in February 2026, so stock QEMU 10.0.2 cannot
use it without an additional patch set.

## Evidence and pinned sources

- [QEMU 10.0.2 Zynq machine documentation](https://www.qemu.org/docs/master/system/arm/xlnx-zynq.html)
- [Analog Devices 2018_R1 Linux driver tree](https://github.com/analogdevicesinc/linux/tree/2018_R1)
- [ADI AD9361 reference manual UG-570](https://www.analog.com/media/en/technical-documentation/user-guides/ad9361.pdf)
- [ADI AXI AD9361 HDL interface and channel documentation](https://analogdevicesinc.github.io/hdl/library/axi_ad9361/index.html)
- [ADI AXI DMAC synthesis parameters and register contract](https://analogdevicesinc.github.io/hdl/library/axi_dmac/index.html)
- [ADI physical-device calibration trace (983.04 MHz BBPLL, divide 9)](https://ez.analog.com/rf/wide-band-rf-transceivers/design-support/f/q-a/570178/ad9361-digital-tuning-error-on-linux/495314)
- [ADI wide-band calibration trace (25.215 MHz effective BBBW)](https://ez.analog.com/wide-band-rf-transceivers/design-support/f/q-a/100276/ad9364-close-in-phase-noise-variation)
- [AMD QEMU co-simulation guide](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/862421112/Co-simulation)
- [Xilinx QEMU](https://github.com/Xilinx/qemu)
- [libsystemctlm-soc Remote-Port protocol](https://github.com/Xilinx/libsystemctlm-soc/blob/master/docs/remote-port-proto.md)
- [Zynq PL Remote-Port device-tree overlay](https://github.com/Xilinx/qemu-devicetrees/blob/master/zynq-pl-remoteport.dtsi)
- [Remote-Port upstream RFC/patch series](https://lists.gnu.org/archive/html/qemu-devel/2026-02/msg01760.html)

Exact addresses and probe contacts are machine-readable in
[`p210-contacts.json`](p210-contacts.json).
The FFT register ABI and memory ordering are specified separately in
[Firmware's canonical `P210_FFT_ABI.md`](https://github.com/PhysicistJohn/Atom-NeptuneSDR-Firmware/blob/main/docs/P210_FFT_ABI.md).

## License boundary

The device sources and headers under `cosim/qemu-10.0.2/`, and the integration
diff under `qemu/patches/`, are QEMU integration work distributed under
GPL-2.0-or-later as marked in the copied source files. The repository's
top-level MIT license applies to the independently written twin tooling; it
does not relicense QEMU or the files compiled into QEMU. The GPL text is
retained at [`../LICENSES/GPL-2.0-or-later.txt`](../LICENSES/GPL-2.0-or-later.txt).
