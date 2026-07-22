# Architecture: contracts at the contacts

## Goal

The twin is built around a testable definition of equivalence: two implementations are interchangeable for a declared use when every observable contact used by that environment satisfies the same contract.

That is intentionally narrower and more useful than claiming that Python objects reproduce every transistor, ARM instruction, FPGA timing path, or RF electromagnetic field. Internal implementation may differ; the externally consumed behavior must not.

## Decomposition

The target is split where the physical board already has meaningful protocol, clock, power, or signal boundaries:

```text
50-ohm SMA
    <-> RF frontend <-> AD9361 <-> DDR IQ lanes <-> FPGA PL / AXI DMAC
                                      |                 <-> Zynq firmware / IIO
                                      +-> FFT/average/packet spectrum path
USB VBUS -> clock and power -----------------------/          |
                                                              v
host application <-> libiio <-> USB composite or Ethernet <-> IIOD
```

The implementation is also split across two public repositories at a deliberate
release boundary:

| Repository | Owns | Does not own |
| --- | --- | --- |
| [`Atom-NeptuneSDR-Twin`](https://github.com/PhysicistJohn/Atom-NeptuneSDR-Twin) | QEMU machine/devices, behavioral and numerical reference models, USB/network emulation, host protocol clients, contract graph, orchestration and verification | ARM source, firmware downloads, rootfs construction, or the canonical board-side ABI |
| [`Atom-NeptuneSDR_Firmwave`](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave) | ARM source, immutable input locks, download/audit/build tooling, XSA validation, QEMU-development rootfs composition, firmware tests, machine-readable firmware interface, and canonical FFT ABI | QEMU device implementation, RF/reference behavior, USB appliance, or acceptance verdict |

The repositories do not import each other's Python packages. Firmwave publishes
a versioned interface and a non-flashable runtime manifest; the Twin models
that interface and independently verifies every emitted artifact before boot.
`deps/firmwave.lock.json` binds URL, full commit,
tree, and interface SHA-256. `scripts/resolve_firmwave.py` accepts only that
identity from `../Atom-NeptuneSDR_Firmwave`,
`NEPTUNESDR_FIRMWAVE_ROOT`, or its own managed cache. This makes the repository
split a checked contact rather than an informal path convention.

The executable contract graph in `specs/contracts.json` contains eight components:

| Component | Responsibility | Principal contacts |
| --- | --- | --- |
| `board_clock_power` | USB input power, regulated-rail assumption and AD9361 reference clock | VBUS, 40 MHz reference |
| `rf_frontend` | SMA routing, matching and board-level analog impairments | 50-ohm RX/TX, chip RF pins |
| `ad9361` | SPI registers, ENSM, calibration, gain, LO, bandwidth and 2x2 baseband words | SPI, GPIO, reference clock, RF, 12-bit IQ |
| `fpga_pl` | IQ capture/playback, register ABI, buffering and DMA | AD9361 IQ, AXI4-Lite, DMA |
| `pl_fft_pipeline` | Optional wideband refinement: block FFT, power averaging, backpressure/loss reporting and spectrum packets | IQ DMA, AXI4-Lite, NSFT-v1 DMA |
| `zynq_firmware` | Boot chain, device tree, kernel drivers, bitstream ABI and IIO service | AXI, DMA, SPI/GPIO, UART, IIO buffers |
| `usb_gadget` | Enumeration, composite functions, native IIO pipes and bus reset | IIO buffers, USB 2.0 |
| `host_libiio` | Host discovery, control and application sample boundary | USB, libiio API, complex IQ |

This layout isolates uncertainty. For example, a corrected RF impairment model can replace `rf_frontend` without changing USB, while a captured descriptor tree can refine `usb_gadget` without pretending RF calibration improved.

The FFT path is a decomposed contact inside the PL target rather than a claim
that the pinned public bitstream already contains this feature. Its input
contract budgets aggregate complex samples against stream-clock lanes; its
output contract specifies scaling, bin order, sequence/counters and the `NSFT`
version 1 wire packet. The Python radix-2 implementation remains a numerical
oracle. The firmware-executing runtime maps a stable PL ABI at `0x7c450000`:
the ADI driver and AXI-DMAC produce a real Linux IIO block, ARM copies that
completed block into a QEMU-reserved DDR window, starts a deterministic integer
1,024–65,536-point FFT device, converts its power bins, and transmits
CRC-checked NSFT packets over the emulated GEM. This is executable block-level
firmware/PL integration evidence, not zero-copy or sustained-rate evidence and
not proof that the public P210 bitstream contains the block. A physical XC7Z020
implementation must separately provide a direct stream or DMA-safe kernel
buffer contract and close HDL synthesis, DSP/BRAM use, cache coherency,
clock-domain crossings and post-route timing.

The reference PL runtime completes the continuous contact independently of
wall-clock accelerator speed. It owns the RF source, consumes consecutive
simultaneous RX1/RX2 blocks, derives rate/LO/configuration epoch atomically,
and emits channel-0/channel-1 NSFT packets as one indivisible update. A
retune-truncated average is counted and marked in the next pair. A full bounded
result queue stalls RF consumption; it never silently overwrites an update.
Its sample-time trace is deterministic, while its snapshot separately reports
whether the dependency-free implementation leads or lags wall time.

The USB contact is similarly decomposed from the absent electrical PHY. The
public P210 device tree and QEMU remain host-mode. A USB/IP adapter exports the
reference composite device to a standard Linux virtual host controller and
can bridge native-IIO bytes to the released guest `iiod`. This changes the
transport implementation without weakening descriptor, EP0, bulk-pipe, reset,
mass-storage, CDC or RNDIS contact contracts.

## Assume/guarantee contracts

A component contract is a pair `C = (A, G)` over typed contacts and modes:

- `A`, the assumptions, states the environment in which the component promises to work.
- `G`, the guarantees, states what the component produces when those assumptions hold.

An implementation refines a specification when it accepts at least the specified environments and produces no behavior outside the specified guarantees. In set terms:

```text
A_spec is a subset of A_impl        (the implementation assumption is weaker)
G_impl is a subset of G_spec        (the implementation guarantee is stronger)
```

Composition connects compatible producer and consumer ports and discharges an internal assumption only when another guarantee entails it. Port kind, direction, protocol, value domain, word width, byte order, sample rate, and clock domain are checked. An unproved implication fails closed; it is not silently called compatible.

This matters at the FPGA/firmware seam and at the repository seam. A matching
filename is insufficient. The bitstream register ABI, device-tree addresses,
kernel drivers, DMA format, lane order, overflow semantics, Firmwave Git/tree
identity, canonical interface hash, and runtime artifact hashes must be
coherent as one contract.

## Contact equivalence

Different contacts require different equality relations:

| Contact class | Equivalence relation | Typical evidence |
| --- | --- | --- |
| Static deterministic | Byte equality after documented normalization | USB descriptors, FDT facts, IIO XML, firmware hash |
| Stateful deterministic | Same accepted inputs, transitions, errors and outputs | SPI, ENSM, EP0 requests, IIOD commands |
| Timed | Normalized event traces agree within declared tolerances | boot, calibration, DMA completion, USB reset |
| Streaming | Same packing/order plus explicit loss, backpressure and discontinuity behavior | RX/TX IQ buffers, FIFO counters |
| Reduced spectrum | Same FFT convention, bin selection, encoding, sequence/counter metadata and CRC | NSFT packets and RTL/software golden vectors |
| RF analog | Metrics or distributions agree within a calibrated envelope | gain, ripple, NF, EVM, isolation, phase, drift |

The deterministic model uses monotonic sample/virtual time. Equal inputs yield
equal contact traces and content-addressed quiescent snapshots. Optional
listener threads and the continuous worker read wall time only for I/O
deadlines, pacing and an explicit lag metric; wall time never labels RF samples
or changes numerical results. Hardware traces are normalized before
differential comparison because host bus numbers, wall-clock timestamps and
serial numbers can legitimately vary.

## Evidence is attached to guarantees

Evidence is not a single project badge. Each guarantee carries a required level from `E0_CLAIM` through `E5_CALIBRATED`; see [EVIDENCE.md](EVIDENCE.md). A USB descriptor may reach E4 while RF phase coherence remains E0. The project-wide exactness claim can never be stronger than the weakest guarantee needed by the intended use.

## Differential closure loop

The route from reference model to delivered-unit twin is:

1. Capture immutable inputs and provenance: listing, photos, descriptors, IIO schema, firmware hashes and tool versions.
2. Replay the same stimulus against the model and unit at one contact at a time.
3. Normalize only declared nondeterminism such as wall time or host-assigned USB address.
4. Store both raw traces, the normalizer version and a machine-readable diff.
5. Refine the smallest responsible component; do not patch downstream symptoms.
6. Add the trace as a regression test and promote only the affected guarantee’s evidence level.
7. Calibrate RF metrics over a declared frequency, gain, temperature, bandwidth and power grid.

The arrival script deliberately stops before stimulus that can alter the device. Firmware writes, DFU, register pokes, sample TX, and RF transmission belong to later, separately authorized procedures.

## Failure and uncertainty semantics

- Unknown values remain unknown; advertised values are not promoted to observations.
- Buffer loss is signaled and counted. Silence is not substituted for an unreported overflow.
- Illegal state transitions and out-of-range configurations raise explicit errors.
- Hashes prove byte identity, not authorship, safety, or compatibility.
- Passing acceptance binds both clean repository source identities and the
  Firmwave interface/runtime manifests; substituting an unpinned or dirty
  firmware tree is a failure, not a warning.
- The basic QEMU kernel-entry harness proves only its declared scope.  The
  P210-enabled QEMU machine separately executes the ARM AD9361, CF-AXI,
  four-entry DMAC, IIO/IIOD, GEM, and proposed FFT contacts. The standard
  USB/IP adapter separately executes the virtual USB-device contact. Neither
  layer proves physical RF or electrical USB signaling.
- A valid AD9361 setting does not prove that the board’s RF matching network meets that setting.
- An internally achievable sample rate does not prove sustainable USB or Ethernet throughput.

## Non-goals at the current stage

The project is not an RTL-equivalent Zynq implementation, a cycle-accurate
Cortex-A9 timing model, a transistor-level AD9361 model, an enclosure CAD
twin, a signed vendor firmware replacement, or a calibrated RF instrument.
The P210-enabled QEMU machine is a functional full-system development target:
it executes ARM instructions and real Linux drivers against board-visible
SPI/MMIO/DMA/IRQ contacts, but its RX waveform is a deterministic digital
source and its proposed FFT has no synthesized RTL timing/resource evidence.
The public P210 bundle supplies a kernel/device tree but no complete vendor
rootfs, so the executable userspace is the separately hash-locked official
Pluto v0.39 ARM rootfs. The virtual USB composite device is implemented at the
protocol contact through USB/IP; connector signal integrity, RF, oscillator,
power, and PCB behavior cannot be inferred from the retail listing or this
integration run.
