# Firmware-executing runtime acceptance

## Scope

`scripts/run_p210_firmware.sh` is the pre-arrival hardware-development gate.
The Twin resolves the exact clean
[`Atom-NeptuneSDR_Firmwave`](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave)
revision pinned by `deps/firmwave.lock.json`, asks it to compose the public P210
Linux 4.14 kernel/device tree with the hash-locked official Pluto v0.39 ARM
userspace, independently verifies its non-flashable runtime manifest, and boots
that bundle on the Twin's P210-enabled QEMU 10.0.2 machine. This is an
integration composition, not a representation that the seller supplied or
tested the combined image.

Resolution honors `NEPTUNESDR_FIRMWAVE_ROOT` when set and otherwise checks
`../Atom-NeptuneSDR_Firmwave`; if neither is present it can clone the pinned
revision into `.cache/deps/firmwave/`. `python3 scripts/resolve_firmwave.py
--offline` prohibits that fetch. User-managed checkouts are never modified,
and an origin, commit, tree, interface-hash, or cleanliness mismatch fails the
gate.

The default acceptance run must prove all of these contacts in one boot:

1. both Cortex-A9 CPUs are online;
2. the released ADI AD9361, CF-AXI DDS, CF-AXI ADC, and AXI-DMAC drivers probe;
3. released ARM `iiod` 0.26 is reachable with the pinned official host libiio
   client and exposes the expected five-device context;
4. the driver accepts the 61.44-MSPS sample rate and 50-MHz RX bandwidth;
5. one nonzero, phase-coherent 65,536-frame 2x2 block crosses the real Linux
   IIO buffer interface;
6. ARM copies that completed block into the reserved FFT input window and
   drives the FFT 1.0 MMIO/DMA ABI after checking its capabilities and limits;
7. the accelerator returns 131,072 power bins and matching sequence metadata;
8. ARM emits two complete NSFT-v1 packets, and the host validates their CRCs,
   sizes, encoding, identical per-update sequence/timestamp/configuration/loss
   metadata, and independent expected tone bins; and
9. the bounded run terminates QEMU and releases both forwarded TCP ports.

The run writes its serial log, QEMU diagnostic log, libiio context, NSFT wire
capture, decoded JSON report, and provenance manifest below
`.cache/p210-runtime/`. Inputs and derived images are SHA-256 recorded in the
manifest; the wire-capture hash varies because packet timestamps are live.
The full acceptance manifest also binds both repository commits/source-state
hashes, Firmwave's canonical interface hash, its runtime manifest, and the
independently rehashed artifacts. A result from a different or dirty Firmwave
tree cannot be promoted to a pass.

## Current deterministic acceptance vector

| Contact | Required result |
| --- | --- |
| RX profile | 61,440,000 samples/s; 50,000,000 Hz RF bandwidth |
| Scan frame | time-major `I0,Q0,I1,Q1`, signed 12 significant bits in four 16-bit little-endian containers |
| Capture | 65,536 frames, 524,288 bytes |
| FFT | 65,536 bins × two channels, rectangular, per-stage scaled, natural order |
| RX1 tone | bin 5,120; +4.8 MHz; approximately -2.53 dBFS |
| RX2 tone | bin 13,312; +12.48 MHz; approximately -6.08 dBFS |
| Spectrum wire update | two CRC-checked packets; 262,288 bytes total |
| Host endpoints while serving | `ip:127.0.0.1:30431` (`iiod`), `tcp:127.0.0.1:30432` (NSFT) |

The default profile starts at a 2.4-GHz RX LO and configuration epoch zero.
The ARM service reads the live AD9361 RX LO, sampling frequency, and RF
bandwidth immediately before and after every IIO block. If those values change
during capture, that block is discarded and retried; a stable changed profile
increments the configuration epoch and its actual sample rate and center
frequency are carried in both packets. The packet timestamp is taken at block
capture completion, before FFT processing.

The FFT endpoint intentionally has one active client. Additional connections
wait in the listener queue, and a client that does not drain its TCP receive
stream is disconnected after the two-second per-update send deadline. The
evidence collector retains exactly the selected channel-0/channel-1 packet
bytes, even when TCP also delivered bytes from a later update in the same
receive call.

## Known allowed boot diagnostics

The launcher treats these as explicit model limitations, not silent success:

- the public device tree puts USB in host mode and the QEMU machine has no
  matching USB PHY/gadget UDC, so `ci_hdrc` fails with `-110`;
- two `adf4350` optional external-synthesizer nodes fail their `muxout` probes;
  they are not in the tested internal-LO RX path;
- emulated OCM suspend, RTC, jitter-entropy, and Avahi discovery are absent;
- the GEM model warns that it cannot synthesize the requested 25-MHz target
  clock, but the fixed PHY links and the libiio/NSFT TCP exchanges must pass;
  and
- the unused second GEM instance has no network peer.

Any new serious kernel/QEMU diagnostic is a regression until it is understood
and deliberately added to the allowlist. USB, external synthesizers, suspend,
RTC, and mDNS are therefore not acceptance claims.

## Layer boundary

This run is block-oriented. Linux AXI-DMAC fills its IIO buffer, then ARM copies
the completed block to a separate FFT DMA window. The guest stops and restarts
the IIO buffer for each update and does not report an uninterrupted input
sequence. The separate continuous reference-PL runtime establishes consecutive
2x2 sample indices, retune-atomic averaging, a bounded result queue and explicit
lag/loss semantics at the same NSFT contact. It does not pretend that a
dependency-free host model is a post-route, wall-clock 61.44-MSPS FPGA.

The fixed `/dev/mem` windows are isolated from Linux with `mem=384M` only in the
QEMU harness. Physical deployment requires a device-tree reservation and a
kernel driver using DMA-safe allocation/cache synchronization, or a direct PL
streaming design. The FFT device is executable virtual hardware but is not
synthesized RTL; XC7Z020 DSP/BRAM use, CDC, timing closure, and the board's
50-MHz RF passband remain arrival/implementation gates.

The public device tree's host-mode USB controller remains outside this QEMU
runtime. The complete appliance composes it with the standard USB/IP device
adapter; native IIO can bridge to this same released guest `iiod`. See the
[complete appliance](VIRTUAL_APPLIANCE.md), [USB](USB.md), the
[canonical Firmwave FFT ABI](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave/blob/main/docs/P210_FFT_ABI.md),
and the [50-MHz plan](WIDEBAND_50MHZ.md).
