# Evidence and provenance

## Evidence ladder

The enum names below are executable in `neptunesdr_twin.contracts.EvidenceLevel`. Levels apply to individual guarantees, not to the repository as a whole.

| Level | Meaning | Minimum artifact |
| --- | --- | --- |
| `E0_CLAIM` | A hypothesis, retail claim, inferred target, or unverified transcription | Claim text plus source and access date |
| `E1_STATIC` | Static structure has been inspected without executing the behavior | Parsed descriptor, source review, data-sheet fact, image metadata or hash |
| `E2_SIMULATION` | The behavior passes deterministic model tests | Reproducible test vector, result, trace and model revision |
| `E3_INTEGRATION` | Multiple real software/firmware components interoperate in a controlled harness | Tool versions, configuration, logs and content-addressed inputs |
| `E4_DIFFERENTIAL` | The twin and the purchased unit agree under the same contact stimulus | Raw unit trace, raw twin trace, declared normalization and diff |
| `E5_CALIBRATED` | Agreement is quantified against traceable equipment over a declared operating envelope | Calibration records, uncertainty, sweep grid, metrics and confidence/tolerance |

Higher evidence does not erase lower-level source material. Raw artifacts are retained so a conclusion can be audited after tools or interpretations change.

## Current provenance map

| Repository artifact | Present basis | What it does not prove |
| --- | --- | --- |
| `src/neptunesdr_twin/data/p210.json` | User-supplied listing, HAMGEEK page, public board artifacts and resolved modeling choices | The chips, DDR, clock, RF paths or revision in the delivered unit |
| `specs/contracts.json` | Engineering decomposition and acceptance targets | That every target is satisfied by hardware |
| `src/neptunesdr_twin/data/usb-p210-observed.json` | Byte transcription of a supplied/reference high-speed descriptor map | That the purchased unit has the same firmware, descriptor tree or second USB bridge |
| `src/neptunesdr_twin/data/firmware-lock.json` | URL, size and SHA-256 for pinned public artifacts | Vendor authorship, authenticity, freedom from defects, or compatibility with this revision |
| Unit tests | Deterministic E2 evidence for implemented model behavior | Hardware equivalence |
| Basic firmware extraction/QEMU harness | P210 `kernel-entry-only` or Pluto `kernel-and-initramfs-entry` software evidence | FPGA bitstream execution, AD9361/RF/DMA behavior, physical USB enumeration or a P210 rootfs |
| P210-enabled QEMU machine and ARM FFT runtime | E3 integration of the public P210 kernel/DT, official Pluto v0.39 rootfs/`iiod`, real ADI Linux drivers, emulated AD9361/CF-AXI/four-entry DMAC/GEM, nonzero 2x2 IIO block capture, CPU copy to reserved DDR, and proposed 65,536-point FFT/NSFT path | Sustained/lossless 61.44-MSPS processing, zero-copy/direct-PL streaming, vendor-shipped rootfs identity, the public bitstream containing the proposed FFT, synthesized XC7Z020 timing/resources, physical RF/clock/power, or USB gadget enumeration |
| `scripts/capture_unit.sh` output | Future E1/E4 input from the purchased unit | RF performance; the script intentionally performs no sample capture or TX |
| Future conducted sweeps | E5 input when equipment and uncertainty are recorded | Behavior outside the measured envelope |

The USB fixture’s date or label must not be read as a delivered-unit capture. Until a new capture is made directly from the purchased board, it is a reference regression fixture.

## Known claim conflicts

These conflicts are requirements for measurement, not inconveniences to smooth over:

| Field | Evidence in hand | Current modeling decision | Closure test |
| --- | --- | --- | --- |
| DDR capacity | The same HAMGEEK page says `512M`, `1GByte`, and `512MB 16Bit` | Model 512 MiB; retain conflict | Boot log, `/proc/meminfo`, device tree, DDR part marking if accessible |
| CPU clock | Storefront says 766 MHz; pinned public XSA configures 666.666687 MHz | Model XSA value; retain 766 MHz claim | Boot clock summary plus measured/reported clock on the delivered unit |
| RF transceiver | Current listing says AD9361; a public P210 owner documented an AD9363-marked board | Target AD9361 because that is the purchased SKU description; no unit claim | Package marking, IIO model, device tree, register identity and RF envelope |
| 2R2T operation | Listing promises 2T2R; the same field report did not establish the second pair | Model the intended 2x2 contact and mark hardware proof pending | Two-channel IIO schema, DMA lane test and independent conducted RF tests |
| 50/56 MHz bandwidth | AD9361 silicon supports up to 56 MHz; board matching and host path are unknown | Permit internal 50 MHz profile at 61.44 MSPS | Conducted passband/alias sweep plus loss-free burst test |
| Host rate | Listing says 12 MSPS host and 61.44 MSPS burst without channel, interface, format or duration scope | Treat 12 MSPS as an E0 one-channel budgeting basis only | USB and Ethernet rate sweep for 1/2 channels with overflow counters |
| USB personality | Reference fixture is `0456:b673`; firmware and second-port bridge vary by revision | Byte-lock fixture without calling it the unit | Capture each physical port, speed, strings, interfaces and endpoints |
| Oscillator | Listing says 0.5 ppm; nominal context uses 40 MHz | Nominal 40 MHz, board guarantee withheld | Frequency-counter or calibrated RF carrier measurement across temperature |

## Source register

Accessed 2026-07-14 unless stated otherwise.

### Direct product and user evidence

- User-supplied receipt image and verbatim listing text in the project conversation. This establishes purchase intent and SKU wording, but the receipt is not copied into the repository because it contains personal/order metadata.
- [HAMGEEK P210 product page](https://www.hgeek.com/products/hamgeek-p210-70mhz-6ghz-sdr-development-board-aluminum-alloy-shell-zynq7020-ad9361-with-open-source-code-for-pluto-sdr). This is the direct retail source for SKU 95157 and its contradictory 512 MB/1 GB memory statements, 2T2R, 12 MSPS host and 61.44 MSPS burst claims.

### Primary component and software sources

- [Analog Devices AD9361 product page and Rev. G data sheet](https://www.analog.com/en/products/ad9361.html): 2x2 transceiver, RX 70 MHz–6 GHz, TX 47 MHz–6 GHz, under 200 kHz–56 MHz tunable channel bandwidth, 12-bit conversion and CMOS/LVDS interface.
- [AD9361 Reference Manual UG-570](https://www.analog.com/media/en/technical-documentation/user-guides/ad9361.pdf): ENSM, calibration, clock/filter and digital-interface behavior.
- [AMD Zynq-7000 overview DS190](https://docs.amd.com/v/u/en-US/ds190-Zynq-7000-Overview): XC7Z020 processing-system and programmable-logic family facts.
- [libiio documentation](https://analogdevicesinc.github.io/libiio/): local and remote IIO context model and network/USB/serial backends.
- [ADI direct libiio access for Pluto](https://analogdevicesinc.github.io/documentation/solutions/platforms/pluto/setup/libiio.html): `iio_info`, context attributes, devices and network URI behavior.
- [ADI Pluto USB enumeration overview](https://analogdevicesinc.github.io/documentation/solutions/platforms/pluto/get-started/unboxing.html): Ethernet, mass-storage, serial-console and IIO functions in the reference Pluto stack.
- [ADI Pluto firmware repository](https://github.com/analogdevicesinc/plutosdr-fw/tree/v0.39): source/build topology and v0.39 release inputs. The repository lock records the release archive digest.
- [Linux USB gadget configfs documentation](https://docs.kernel.org/usb/gadget_configfs.html) and [FunctionFS documentation](https://www.kernel.org/doc/html/latest/usb/functionfs.html): what configfs can configure and why a userspace function must supply its own descriptors/behavior.

### Board-adjacent, non-authoritative sources

- [Independent Neptune SDR P210 field report](https://wucke13.de/posts/neptune-sdr/): a different unit’s chip markings, ports and USB behavior. This is useful counterevidence, not proof about the purchased board.
- [Pinned public P210 SD boot artifact](https://github.com/wucke13/Neptune-SDR-nix-utils/tree/ba4a958333a3dfec5e5102e30fe017508e1fd6f6): third-party artifact referenced by the firmware lock. Its SHA-256 protects the bytes used by tests.
- [Pinned public P210 `system_top.xsa`](https://raw.githubusercontent.com/wucke13/Neptune-SDR-nix-utils/ba4a958333a3dfec5e5102e30fe017508e1fd6f6/pkgs/neptunesdr-xsa-bin/system_top.xsa): SHA-256 `caecfeb3ce96f3c6951311ed985af4f144b5a8e54a3cbc25f0527b02c4ff1066`. Static inspection identifies `xc7z020clg400-1`, Vivado 2023.2 metadata, a 666.666687 MHz APU configuration, 16-bit DDR at a configured 533.333374 MHz using `MT41K256M16 RE-125`, `axi_ad9361`, four 16-bit RX packer lanes, a 64-bit RX DMAC contact, TX/RX AXI DMAC, pack/unpack cores and their register bases. This is valuable PL/PS handoff evidence, not proof that the purchased unit carries the bitstream, not evidence that it contains the new FFT path, and not QEMU-executable firmware.

## Capture provenance requirements

Every unit capture should include:

- UTC timestamp and the operator-assigned unit ID.
- Board/enclosure photos and physical port mapping kept outside public commits if they reveal serials.
- Host OS, kernel, architecture, capture script SHA-256 and tool versions.
- Connection topology: physical port, cable, hub, negotiated USB speed, Ethernet path and power source.
- Raw command output, including failures and exit status; do not paste only successful excerpts.
- A SHA-256 manifest generated after capture.
- Whether a host key was newly accepted and whether SSH was interactive.
- A statement that no firmware write, RF TX, DFU action or attribute write occurred.

Captures may contain device serial numbers, MAC addresses, IP addresses and hostnames. Preserve the raw private copy; create a separately hashed redacted derivative for publication.

## Promotion rules

- Retail wording never rises above E0 by repetition across reseller pages.
- A data sheet can establish a chip capability at E1, not board-level performance.
- Public firmware can reach E3 in an integration harness, but only a hash-matched unit artifact can support differential claims about the purchased board.  This project's E3 runtime is explicitly a composition of the public P210 kernel/device tree and the official Pluto v0.39 rootfs because no public full P210 rootfs was found.
- A QEMU boot log must retain its scope label. P210 kernel entry and Pluto kernel/initramfs entry cannot be promoted into claims about PL, RF, DMA, USB gadget behavior or delivered-unit firmware.
- E4 requires same-stimulus comparison and stored diffs. Merely observing that both tools launch is insufficient.
- E5 requires equipment identity/calibration, cabling and attenuator loss, environmental conditions, test grid, uncertainty and acceptance thresholds chosen before the sweep.
- If the unit differs by PCB, chip, firmware, descriptor, clock or RF network revision, fork the profile. Do not average incompatible revisions into one “exact” model.
