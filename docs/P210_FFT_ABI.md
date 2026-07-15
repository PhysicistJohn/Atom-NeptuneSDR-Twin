# P210 PL FFT accelerator ABI 1.0

This is the stable ARM-visible contract for the NeptuneSDR twin's proposed
XC7Z020 programmable-logic FFT block.  The QEMU device executes the contract;
the register definitions live in
`cosim/qemu-10.0.2/include/hw/misc/p210_fft.h`.

It is a functional development target, not evidence that the public P210
bitstream already contains an FFT, nor that corresponding RTL meets XC7Z020
resource, clock-domain-crossing, or post-route timing requirements.

## Bus contact

- Proposed physical base: `0x7c450000`
- Span: `0x1000` bytes
- Registers: 32-bit, little-endian, naturally aligned accesses only
- Proposed interrupt: Zynq GIC SPI 58, active high/level
- Identity: `ID = 0x5446464e` (the bytes `NFFT` in little-endian memory)
- ABI version: `VERSION = 0x00010000` (major 1, minor 0)

The twin maps this block only in the P210-enabled QEMU machine.  A derived
device tree can bind it to a purpose-built driver or, for bring-up, a UIO node
at the address above with interrupt tuple `<0 58 4>`.  Production firmware
must use DMA-safe memory and perform the cache maintenance required by Zynq's
non-coherent PL/DDR path; a virtual address is not a DMA address.

```dts
fft@7c450000 {
    compatible = "generic-uio";
    reg = <0x7c450000 0x1000>;
    interrupt-parent = <&intc>;
    interrupts = <0 58 4>;
};
```

The pinned 4.14 kernel has the generic UIO platform driver built in; its boot
arguments must include `uio_pdrv_genirq.of_id=generic-uio` for this bring-up
binding.  A dedicated driver should replace UIO when it owns DMA allocation,
cache synchronization, concurrency, and untrusted descriptor validation.

## Registers

| Offset | Name | Access | Reset / meaning |
| ---: | --- | --- | --- |
| `0x000` | `ID` | RO | `0x5446464e` |
| `0x004` | `VERSION` | RO | ABI 1.0 |
| `0x008` | `CAPABILITIES` | RO | IQ16 LE input, uint32 LE power, two channels, per-stage scaling, natural bin order, IRQ |
| `0x00c` | `CONTROL` | RW/pulse | bit 0 `START`, bit 1 `SOFT_RESET`, bit 8 `IRQ_ENABLE`; only bit 8 reads back |
| `0x010` | `STATUS` | RO/W1C | bit 0 `BUSY`, bit 1 `DONE`, bit 2 `ERROR`, bit 3 `IRQ_PENDING`; write 1 to clear `DONE` or `ERROR` |
| `0x014` | `ERROR_CODE` | RO | last start error; retained when `ERROR` is acknowledged |
| `0x018` | `LOG2_N` | RW | FFT length exponent, 10 through 16 |
| `0x01c` | `CHANNEL_COUNT` | RW | input channels, 1 or 2 |
| `0x020` | `CHANNEL_MASK` | RW | nonzero subset of input channels; bit 0/1 selects channel 0/1 output |
| `0x024` | `INPUT_ADDR` | RW | 32-bit physical DDR source address, 4-byte aligned |
| `0x028` | `INPUT_BYTES` | RW | exactly `N * CHANNEL_COUNT * 4` |
| `0x02c` | `OUTPUT_ADDR` | RW | distinct 32-bit physical DDR destination, 4-byte aligned |
| `0x030` | `OUTPUT_BYTES` | RW | exactly `N * popcount(CHANNEL_MASK) * 4` |
| `0x034` | `SEQUENCE` | RW | firmware-supplied request tag |
| `0x038` | `RESULT_SEQUENCE` | RO | request tag copied after successful completion |
| `0x03c/040` | `COMPLETED_LO/HI` | RO | 64-bit successful-transform counter |
| `0x044/048` | `ERROR_COUNT_LO/HI` | RO | 64-bit rejected/failed-transform counter |
| `0x04c` | `BINS_WRITTEN` | RO | total bins written by the last success |
| `0x050` | `MIN_LOG2_N` | RO | 10 (`N=1024`) |
| `0x054` | `MAX_LOG2_N` | RO | 16 (`N=65536`) |

Configuration writes are ignored while `BUSY`.  `START` clears the previous
`DONE`/`ERROR`, validates the complete descriptor, reads the input, performs
the transform, writes the output, copies `SEQUENCE`, and then sets `DONE`.
Firmware must treat this as asynchronous even though the current QEMU model
finishes within the initiating MMIO transaction.  `SOFT_RESET` wins over
`START` and restores configuration and counters to reset values.

The IRQ line is asserted while `IRQ_ENABLE` is set and either `DONE` or
`ERROR` remains set.  Polling and interrupt-driven firmware therefore use the
same W1C acknowledgement.  Read a 64-bit counter high-low-high if concurrent
hardware completions are possible.

## DMA data formats

`N = 1 << LOG2_N`.  Input is a time-major, channel-interleaved array.  Each
complex sample is four bytes: signed 16-bit little-endian I followed by signed
16-bit little-endian Q.  For two channels the byte stream is:

```text
I0[n=0], Q0[n=0], I1[n=0], Q1[n=0],
I0[n=1], Q0[n=1], I1[n=1], Q1[n=1], ...
```

All configured input channels are present even when `CHANNEL_MASK` selects
only one output.  Input and output ranges must not overlap.

Output is channel-major.  Selected channels appear in ascending physical
channel number; each channel contributes `N` unsigned 32-bit little-endian
linear-power bins in natural FFT order `k=0..N-1`:

```text
power[first selected channel][0..N-1],
power[next selected channel][0..N-1]
```

The forward radix-2 transform uses rectangular weighting and divides every
butterfly result by two, giving an overall `1/N` amplitude scale.  Each output
is the saturated integer `real[k]^2 + imag[k]^2`.  Thus an ideal bin-centred
complex tone of IQ amplitude `A` produces peak power near `A^2`; firmware can
derive dBFS for the AD9361 signed 12-bit sample format as
`10*log10(power / 2048^2)`.  The 16-bit IIO slot is a transport container, not
the ADC full-scale range.  Integer CORDIC twiddles and
defined truncation make QEMU results byte-deterministic.  Firmware performs
FFT-shift, averaging, logarithmic encoding, and NSFT packet framing as needed.

## Errors

| Code | Meaning |
| ---: | --- |
| 0 | none |
| 1 | start while busy |
| 2 | unsupported `LOG2_N` |
| 3 | invalid channel count/mask |
| 4 | unaligned DMA address |
| 5 | byte length does not exactly match the descriptor |
| 6 | 32-bit address-range wrap |
| 7 | QEMU working-memory allocation failed |
| 8 | input DMA read failed |
| 9 | output DMA write failed |
| 10 | input and output ranges overlap |

An error never increments `COMPLETED`, never updates `RESULT_SEQUENCE`, and
never advertises bins written.  It increments `ERROR_COUNT`, sets
`ERROR_CODE`, and raises `ERROR` (and the IRQ when enabled).

## Migration and hardware boundary

QEMU migrates configuration, status, request/result sequence values, and both
counters.  FFT execution is synchronous, so `BUSY` is not a migration
boundary.  A physical implementation may take many cycles and may use vendor
FFT/DMA IP internally, but it refines this contract only if packing, scaling,
validation, completion, error, and counter behavior remain compatible.

The automated QEMU test writes real IQ16 into emulated DDR and reads the
result back through this MMIO/DMA device.  Its maximum-size vector executes
two 65,536-point transforms, reports 131,072 bins, and locates exact positive-
and negative-quarter-rate tones in the two channel-major outputs.  That proves
the functional QEMU path; it does not promote the physical-RTL evidence level.
