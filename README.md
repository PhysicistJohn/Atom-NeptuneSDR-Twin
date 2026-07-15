# NeptuneSDR / HAMGEEK P210 digital twin

This repository is a firmware-executing, contract-driven virtual hardware twin
of the advertised HAMGEEK P210 / NeptuneSDR platform.  Its P210-enabled QEMU
machine runs ARM instructions from the public P210 Linux kernel/device tree
against board-visible AD9361 SPI, CF-AXI ADC/DDS, four-entry AXI-DMAC, dual
Cortex-A9, GEM, DDR, interrupt, and proposed PL FFT contacts.  The released ARM
`iiod` and official host libiio operate across that machine; a second reference
layer supplies deterministic contract/RF/USB models and golden vectors.

The tested wideband path is real software integration, not a zero-filled or
userspace-only demo: Linux IIO captures nonzero 2x2 IQ through the ADI drivers
and AXI-DMAC, ARM copies each completed block into the FFT block's reserved DDR
window, starts a 65,536-point two-channel integer FFT through MMIO/DMA, and
transmits two CRC-checked NSFT spectra over the emulated Gigabit Ethernet
contact at a configured 61.44 MSPS / 50 MHz RX profile.

Two boundaries remain non-negotiable.  No complete vendor P210 rootfs was
published with the public kernel, so the executable userspace is the separately
hash-locked official Pluto v0.39 rootfs; this is not a claim that it is the
seller-shipped image.  It is also not yet an exact twin of the purchased unit:
physical USB-device behavior, PCB/RF response, oscillator/power behavior, and
the proposed FFT's synthesized XC7Z020 resource/timing closure require the
delivered board and/or RTL evidence.  See [Evidence and provenance](docs/EVIDENCE.md).

## Run the firmware-executing hardware twin now

On Apple Silicon macOS, the default command builds the pinned P210-enabled
QEMU 10.0.2 machine, builds the ARM FFT service, downloads only hash-locked
firmware inputs, boots two Cortex-A9 CPUs, and exits only after it receives and
CRC-checks a complete two-channel 65,536-bin spectrum:

```sh
scripts/run_p210_firmware.sh
```

The first run needs Xcode Command Line Tools, `curl`, `tar`, and network access;
it builds the native QEMU and ARM toolchains below `.cache/` and does not install
or flash anything. Later runs reuse that cache. The boot/capture phase is
bounded; initial downloads and compilation are not governed by `--timeout`.
A passing acceptance run ends with `P210_RUNTIME PASS` and retains its serial
log, NSFT wire capture, and decoded report below `.cache/p210-runtime/`.

For a persistent development target, leave the VM running in one terminal:

```sh
scripts/run_p210_firmware.sh --serve
```

Then, in another terminal, use the real guest services:

```sh
scripts/build_host_libiio.sh
scripts/host_iio.sh info
scripts/capture_guest_fft.py
```

The forwarded endpoints are `ip:127.0.0.1:30431` for released `iiod` and
`tcp:127.0.0.1:30432` for the ARM-generated NSFT spectrum stream. See
[Pinned host libiio workflow](docs/HOST_LIBIIO.md) and the
[FFT register/DMA ABI](docs/P210_FFT_ABI.md).

This is the executable pre-arrival target. It runs the public P210 kernel and
device tree with the official Pluto v0.39 ARM rootfs because no complete
seller P210 rootfs is public. It therefore confirms that composition, not the
unknown bytes that will arrive in the unit. Ethernet/IIO/block-FFT are exercised
end to end. This firmware harness restarts an IIO buffer and copies one complete
block through the CPU per update; it does not prove continuous, lossless
61.44-MSPS operation. A physical direct-stream or DMA-buffer driver, RF response,
USB-device enumeration, and FPGA synthesis/timing still require board/RTL work.

## Contract/reference model

Python 3.9 or newer is required. The reference layer has no third-party Python
dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Inspect the resolved target, contract composition, 50 MHz rate budget, and reference USB profile:

```sh
neptune-twin info
neptune-twin contracts
neptune-twin wideband
neptune-twin fft-plan
neptune-twin usb
neptune-twin snapshot --boot-source sd --output evidence/twin-snapshot.json
```

Every command is also available as `python -m neptunesdr_twin ...`.

Run the local IIOD endpoint, or inspect the action without opening a listener:

```sh
neptune-twin serve --dry-run
neptune-twin serve --host 127.0.0.1 --port 30431
iio_info -u ip:127.0.0.1
```

The server is a behavioral conformance endpoint, not a promise that every command in every libiio release is implemented. Its supported command and buffer behavior is locked by the test suite.

Firmware and PL handoff inputs are content-addressed and inspected offline; these commands never flash a board:

```sh
neptune-twin fetch-firmware p210-sd-boot build/p210-sd-boot.tar.zst
neptune-twin validate-firmware build/p210-sd-boot.tar.zst

neptune-twin fetch-firmware p210-system-xsa build/p210-system_top.xsa
neptune-twin validate-firmware build/p210-system_top.xsa

neptune-twin fetch-firmware plutosdr-fw-v0.39 build/plutosdr-fw-v0.39.zip
neptune-twin validate-firmware build/plutosdr-fw-v0.39.zip
```

The companion harness uses a content-addressed cache and can prepare or run a bounded QEMU boot smoke test:

```sh
python3 scripts/fetch_firmware.py p210-sd-boot plutosdr-fw-v0.39
python3 scripts/test_firmware.py

# Dry-run: extract and print the network/monitor-disabled QEMU command.
python3 scripts/qemu_boot.py --artifact p210-sd-boot

# Execution is opt-in and requires qemu-system-arm.
python3 scripts/qemu_boot.py --artifact p210-sd-boot --run
```

That bounded `qemu_boot.py` harness remains intentionally narrow: its P210
result is `kernel-entry-only` because the public P210 bundle has no rootfs, and
its stock Pluto result is `kernel-and-initramfs-entry`.  The separate
P210-enabled runtime described above is what executes the AD9361/CF-AXI/DMAC,
real IIO/IIOD, Ethernet, and proposed FFT contacts.  Neither path executes the
public FPGA bitstream or physically enumerates a USB gadget.

## What is modeled

| Surface | Implemented behavior | Exactness status |
| --- | --- | --- |
| Contract system | Typed contacts, assume/guarantee composition, refinement, evidence checks, runtime monitors | Executable and tested |
| Zynq-7020 | QEMU ARMv7 execution, two Cortex-A9s, 512 MiB DDR, SLCR secondary release, GEM and P210 PL address map; plus a fast Python contract model | Functional/instruction accurate enough for the pinned Linux path; not cycle/timing accurate |
| AD9361 | QEMU SPI identity, calibration side effects and clock/status contacts consumed by the real ADI 4.14 driver; Python ENSM/RF control oracle | Real-driver E3 integration; silicon/RF edge cases remain capture-driven |
| RF/sample plane | Real guest IIO/ADI buffer path with deterministic phase-continuous IQ16LE 2x2 tones and four-entry DMAC; richer Python noise/gain/loopback model | Digital packing/DMA proven; analog impairments require E5 calibration |
| On-chip spectrum path | Real QEMU MMIO/DDR accelerator executing deterministic integer radix-2 FFTs through 65,536×2; ARM NSFT-v1 packetizer and host CRC decoder; Python numerical oracle | Firmware-visible E3 block runtime; present guest copies completed IIO blocks through the CPU, while a continuous physical PL stream and synthesized/post-route evidence remain open |
| IIO/IIOD | Unmodified P210 ADI kernel drivers and released ARM `iiod` 0.26 reached by pinned official host libiio; separate behavioral conformance endpoint | Network/control/buffer integration proven against the composition; purchased-unit differential testing pending |
| USB | Byte-locked reference descriptors, strings, normal/DFU metadata, EP0/native-IIO pipe model, and configfs deployment plan | QEMU's P210 DT is host-mode and no gadget UDC is emulated; physical enumeration remains open |
| Firmware/PL artifacts | Hash-locked public P210 kernel/DT/XSA and official Pluto v0.39 rootfs; ELF/ABI audit, derived initramfs, source-built QEMU runtime | Provenance-preserving composition, not seller-authored full firmware, signed replacement, or bitstream execution |
| Throughput | Separate internal, USB 2.0, Gigabit Ethernet and advertised host-rate contracts | Arithmetic is exact; delivered-unit throughput is unmeasured |

The decomposition and the reason contacts—not internal implementation resemblance—define equivalence are described in [Architecture](docs/ARCHITECTURE.md).

## The 50 MHz requirement

The AD9361 data sheet supports a tunable channel bandwidth up to 56 MHz, so a 50 MHz analog configuration is plausible. That does **not** mean the board can continuously move raw 50 MHz-wide, two-channel IQ to a host.

At 61.44 MSPS with two channels and native 16-bit I/Q containers, the raw payload is 491.52 MB/s (3.932 Gb/s). That is above both USB 2.0’s 60 MB/s signaling ceiling and Gigabit Ethernet’s 125 MB/s line-rate ceiling before protocol overhead. The listing’s “12 MSPS with HOST” and “61.44 MSPS burst” claims are therefore treated separately. Continuous wideband work must process, trigger, decimate, or channelize in the FPGA and use bounded burst capture for undecimated IQ.

The default architecture budget is a 65,536-bin, two-channel on-chip FFT at 61.44 MSPS. Rate-limiting/averaging to 20 spectrum updates/s and emitting 16-bit log-power bins in framed `NSFT` version 1 packets (network byte order with CRC32) would reduce full-spectrum egress to roughly 5.24 MB/s. `neptune-twin fft-plan` proves that declared arithmetic/contact budget. The current ARM acceptance service emits unaveraged, discontinuous blocks and makes no 20-Hz cadence guarantee; an actual Vivado implementation still needs a direct sample path, synthesis, resource, CDC and post-route timing evidence.

The composed twin can publish the same self-framing NSFT byte stream over TCP with `NeptuneSDRTwin.start_spectrum_publisher()` and decode arbitrary TCP chunks with `SpectrumStreamDecoder`. The intended deployed path is physical Gigabit Ethernet or USB-RNDIS. TCP is deliberate: a full 65,536-bin packet—especially float32—does not fit in one UDP datagram.

Read [50 MHz wideband plan](docs/WIDEBAND_50MHZ.md) before buying test gear or designing around this bandwidth.

## Capture the delivered unit first

The capture script only inventories host USB state, IIO metadata, and an optional fixed set of read-only SSH facts. It does not stream samples, enable TX, enter DFU, dump flash, or write device attributes.

```sh
./scripts/capture_unit.sh --output evidence/captures/arrival

./scripts/capture_unit.sh \
  --output evidence/captures/arrival-network \
  --iio-uri ip:pluto.local
```

SSH capture is opt-in. The default is noninteractive and requires an already trusted host key and key-based authentication:

```sh
./scripts/capture_unit.sh \
  --output evidence/captures/arrival-ssh \
  --iio-uri ip:pluto.local \
  --ssh-host root@pluto.local
```

For a new host key or password prompt, add the corresponding explicit options shown by `./scripts/capture_unit.sh --help`. Captures contain serial numbers, MAC addresses, hostnames, and network configuration; review them before publishing.

Follow [Arrival checklist](docs/ARRIVAL_CHECKLIST.md) and request the vendor sources using [the prepared email](scripts/request_vendor_materials.md) before changing firmware.

## Exactness boundary

“Exact” is scoped per contact and per guarantee:

- Deterministic contacts can be byte-exact after normalization: USB descriptors, SPI transactions, IIO schemas, sample packing, boot artifacts and state traces.
- Timed contacts can be trace-equivalent within a stated tolerance: reset, enumeration, calibration, DMA and boot sequencing.
- RF contacts can only be metric- or distribution-equivalent over a declared frequency, gain, temperature and power envelope.
- Manufacturing details, undocumented silicon behavior, enclosure geometry, oscillator error and RF matching are not inferred from a product title.

Current listing conflicts are retained rather than hidden. In particular, the product page says both 512 MB and 1 GB DDR and a 766 MHz CPU, while the pinned public P210 XSA configures 512 MiB-class 16-bit DDR and a 666.666687 MHz Cortex-A9 clock. A third-party P210 field report also found an AD9363-marked unit despite the current listing saying AD9361. This model resolves CPU/DDR toward the public XSA and AD9361 toward the purchased SKU description while requiring delivered-unit confirmation.

Unit conventions are explicit: a byte is 8 bits; the external DDR data bus/transfer word is 16 bits (2 bytes); each I or Q converter component has 12 significant bits in a signed 16-bit container; and one complex I/Q sample occupies 4 bytes per enabled channel. The public XSA’s four 16-bit RX packer lanes form a 64-bit DMA word for one two-channel sample-time frame. “16-bit DDR” is a memory-bus width, not ADC precision or DMA width.

## USB gadget testing

[`scripts/linux_usb_gadget.sh`](scripts/linux_usb_gadget.sh) emits a Linux configfs plan by default and changes nothing. `--apply` is required before it creates or binds a gadget. Kernel-generated RNDIS/ACM/mass-storage descriptors plus a userspace FunctionFS IIO service are needed to approach the observed composite device; configfs metadata alone cannot guarantee byte-identical enumeration. See [USB behavior and deployment](docs/USB.md).

## Project map

- [`specs/contracts.json`](specs/contracts.json): decomposed contact contracts and evidence thresholds.
- [`src/neptunesdr_twin/data/p210.json`](src/neptunesdr_twin/data/p210.json): resolved target facts, conflicts and unknowns.
- [`src/neptunesdr_twin/data/usb-p210-observed.json`](src/neptunesdr_twin/data/usb-p210-observed.json): byte-locked reference USB fixture.
- [`src/neptunesdr_twin/data/firmware-lock.json`](src/neptunesdr_twin/data/firmware-lock.json): pinned firmware inputs and SHA-256 digests.
- [`scripts/run_p210_firmware.sh`](scripts/run_p210_firmware.sh): one-command bounded firmware/IIO/DMA/FFT/NSFT acceptance run or persistent development VM.
- [`scripts/build_p210_qemu.sh`](scripts/build_p210_qemu.sh), [`qemu/patches/0001-p210-zynq-devices.patch`](qemu/patches/0001-p210-zynq-devices.patch): pinned native QEMU build and P210 Zynq machine integration.
- [`firmware/neptune_fft_streamer.c`](firmware/neptune_fft_streamer.c): static ARM capture, accelerator-control, and spectrum-transport service.
- [`cosim/qemu-10.0.2`](cosim/qemu-10.0.2): AD9361, CF-AXI, four-entry AXI-DMAC, and FFT device implementations.
- [`scripts/fetch_firmware.py`](scripts/fetch_firmware.py), [`scripts/test_firmware.py`](scripts/test_firmware.py), [`scripts/qemu_boot.py`](scripts/qemu_boot.py): host-only locked download, validation and opt-in bounded boot smoke tests.
- [`tests`](tests): executable behavior and regression contracts.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): decomposition and contract theory.
- [`docs/EVIDENCE.md`](docs/EVIDENCE.md): evidence ladder, sources and conflicts.
- [`docs/P210_FFT_ABI.md`](docs/P210_FFT_ABI.md): stable ARM-visible FFT register and DMA contract.
- [`docs/HOST_LIBIIO.md`](docs/HOST_LIBIIO.md): pinned official host-client workflow.
- [`docs/RUNTIME_ACCEPTANCE.md`](docs/RUNTIME_ACCEPTANCE.md): hard end-to-end gates, deterministic vector, accepted diagnostics, and physical-deployment boundary.
- [`docs/WIDEBAND_50MHZ.md`](docs/WIDEBAND_50MHZ.md): bandwidth/throughput math and RF test plan.
- [`docs/USB.md`](docs/USB.md): reference personalities, host access and gadget limitations.

## Safety

Keep first-arrival work RX-only. Do not connect a TX port directly to an RX port, do not transmit into an antenna during bench validation, and do not exceed the conservative input envelope in the contracts. Use a 50-ohm conducted setup, rated attenuators, a dummy load, and independently verified power levels. Operation must comply with the radio rules applicable at the test location.

This project does not automatically flash firmware or claim vendor authorization to use Analog Devices’ USB VID on a redistributed product.
