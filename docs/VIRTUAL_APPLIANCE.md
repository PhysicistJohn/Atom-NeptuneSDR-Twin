# Complete virtual appliance

## Completion criterion

The pre-arrival twin is complete when software written for the declared P210
contacts can be developed and tested without the purchased board.  Completion
is therefore judged at contacts, not by reproducing the board's construction:

| Development contact | Virtual implementation | Acceptance evidence |
| --- | --- | --- |
| ARM/Zynq firmware | Dual-Cortex-A9 P210 QEMU machine with 512 MiB DDR and the public P210 device tree | Pinned Linux boots both CPUs and probes the released ADI drivers |
| AD9361 control | SPI/register/calibration model plus a deterministic 2x2 sample source | The released AD9361 driver accepts 61.44 MSPS and 50 MHz RX bandwidth |
| AXI sample plane | XSA-matched CF-AXI and AXI-DMAC MMIO, DMA, IRQ, queue and migration behavior | Linux IIO receives a nonzero, phase-coherent 524,288-byte block |
| Large on-chip FFT | Firmware-visible 1,024–65,536-point integer FFT accelerator and continuous reference PL dataflow | ARM drives the MMIO/DMA ABI; the reference dataflow preserves consecutive 2x2 blocks, retune epochs and bounded backpressure |
| Spectrum egress | Paired, CRC-checked NSFT-v1 packets over TCP | Host verifies two 65,536-bin channel packets and independent tone bins |
| Ethernet/libiio | QEMU GEM with the released ARM `iiod` and pinned official host libiio | Five-device context and live radio retuning pass end to end |
| USB device | Standard USB/IP export of the observed six-interface composite personality | EP0, native IIO, read-only mass storage, CDC ACM, and RNDIS DHCP/ARP/ICMP/TCP-IIOD at 192.168.2.1 are protocol-tested |
| Debug | UART1 TCP console plus retained logfile; optional loopback-only QEMU GDB remote | Firmware console/debug is usable without asserting a revision-unknown USB-UART/JTAG bridge chip |
| Reset and saved state | Deterministic reference lifecycle plus QEMU reset/migration coverage | Reset, IRQ state, queued DMA and error behavior are regression-tested |

That is a whole virtual development target.  It is deliberately layered:

- [`Atom-NeptuneSDR_Firmwave`](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave)
  owns the ARM source, immutable firmware inputs, validation/build tools,
  canonical FFT ABI, and assembly of the explicitly non-flashable
  `qemu-development` bundle.
- This Twin owns QEMU and the board-visible devices that execute the real ARM
  instructions, public P210 kernel/device tree, released ADI drivers and
  official Pluto userspace.
- The deterministic PL/RF reference layer supplies continuous sample-time
  behavior and numerical/wire-format oracles that QEMU is not intended to run
  at wall-clock RF rate.
- USB/IP supplies the host-visible device transport missing from QEMU's
  host-only Zynq ChipIdea controller.  Native IIO can terminate in the local
  twin or bridge unchanged to the released `iiod` inside QEMU.

No board is required for any of those virtual behaviors.  A future unit
capture is a differential-refinement input, not a prerequisite for using the
twin.

The repository boundary is enforced. `deps/firmwave.lock.json`
pins Firmwave's public URL, full commit, tree, and interface SHA-256. The
resolver accepts an exact clean `../Atom-NeptuneSDR_Firmwave` sibling, an exact
checkout named by `NEPTUNESDR_FIRMWAVE_ROOT`, or an automatically populated
Twin-owned `.cache/deps/firmwave/` checkout. It never changes a user-managed
checkout. The runtime gate independently rehashes the interface, manifest, and
bundle artifacts, and full acceptance records both source identities.

## Run it

Install the dependency-free Python package once:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For the deterministic continuous RF/PL target, including all three host
listeners, run:

```sh
neptune-twin appliance --dry-run
neptune-twin appliance
```

This uses sample time for the 61.44-MSPS path and reports wall-clock lag; it
does not slow the modeled radio clock to disguise host compute cost. A spectrum
client must drain complete paired updates or the bounded PL queue stalls input
with visible counters.

Run the firmware-executing appliance and native-USB bridge as one owned
lifecycle:

```sh
scripts/run_virtual_appliance.sh
```

It exposes released guest `iiod` at `127.0.0.1:30431`, the ARM spectrum service
at `127.0.0.1:30432`, live UART1 at `127.0.0.1:30433`, and USB/IP at
`127.0.0.1:3240`. It exits only when a
service fails or the operator stops it, and tears down both child processes.
After the first build, use `--no-build` for a faster start.
The launcher resolves the locked companion automatically. Use
`python3 scripts/resolve_firmwave.py --json` to inspect the selected checkout or
`--offline` to prohibit a managed clone before starting the appliance.

The equivalent two-terminal form, useful while debugging one layer, is shown
below. USB/IP has no authentication or encryption; binding it to `0.0.0.0`
must be limited by a host firewall to a trusted development LAN. Keep
`--host 127.0.0.1` when the Linux client is local.

```sh
scripts/run_p210_firmware.sh --serve
neptune-twin usbip-serve \
  --host 0.0.0.0 \
  --port 3240 \
  --iiod-backend 127.0.0.1:30431
```

On a Linux host with the standard USB/IP client:

```sh
sudo modprobe vhci-hcd
usbip list -r TWIN_HOST
sudo usbip attach -r TWIN_HOST -b 1-1
```

The exported virtual device then enumerates through Linux's USB stack.  The
native-IIO endpoint speaks the IIOD byte stream used by libiio. The RNDIS
function leases `192.168.2.10/24` and exposes the same IIOD service at
`ip:192.168.2.1`. It does not proxy the separate NSFT spectrum service; that is
available at the appliance's `127.0.0.1:30432` contact. The same
server may be run without `--iiod-backend` for a fast, completely local
contract model.

For one bounded firmware acceptance run instead of a persistent VM:

```sh
scripts/run_p210_firmware.sh
```

Success is the literal terminal line `P210_RUNTIME PASS`; retained evidence is
written below `.cache/p210-runtime/`.

## What the physical unit will add

The arriving unit can add only evidence that a virtual machine cannot create:

- the seller-shipped rootfs and bitstream hashes;
- the exact board-revision USB descriptors and bridge-chip identity;
- conducted RF passband, noise, gain, phase, isolation and oscillator drift;
- power sequencing and thermal behavior; and
- proof that a chosen RTL implementation fits and closes timing on the
  particular XC7Z020 build.

Those measurements may refine a profile or expose a seller revision mismatch.
They do not invalidate the virtual appliance as a development target.  The
repository never substitutes an unmeasured physical assertion for a passing
virtual contact.

## Firmware identity

The public materials do not contain a complete seller-authored P210 firmware
image.  The executable composition is intentionally explicit and hash-locked:
the public P210 kernel, device tree and XSA are combined with the official
Pluto v0.39 ARM rootfs.  This composition has been executed in the P210 QEMU
machine; its Firmwave manifest says `profile: qemu-development` and
`flashable: false`. It is not described as byte-identical to unknown seller
storage and must not be written to the arriving board.

That distinction is provenance, not missing functionality.  Host software can
exercise the advertised control, sample, FFT, Ethernet and USB contacts now.
