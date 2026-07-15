# Delivered-unit arrival checklist

The objective of the first session is to preserve evidence and identify the board without changing it. Keep RF transmit disabled, do not enter DFU, and do not install vendor firmware yet.

## Prepare before opening the package

- Create a private unit ID that is not the retail order number.
- Prepare an ESD-safe surface, 50-ohm terminations for TX ports, a known-good USB data cable, a USB power meter if available, and a current-limited 5 V source only if the vendor confirms the power input.
- Install `lsusb` on Linux or use macOS `system_profiler`; install libiio tools if available.
- Do not connect an antenna to a port whose RX/TX identity is uncertain.
- Do not assume which USB-C connector supplies power. The listing names USB-OTG and USB-UART, while a public report for another revision says only one port powered its board.

## 1. Preserve physical evidence

- [ ] Photograph the sealed package, labels, accessories and cable markings.
- [ ] Photograph every enclosure face and connector label before applying power.
- [ ] Record mass and external dimensions; do not open the enclosure if that affects return or warranty rights.
- [ ] Record antenna/cable count and visible damage.
- [ ] Map the four SMA labels and both USB connectors exactly as printed. Do not infer from left/right alone; photo orientation changes.
- [ ] Note switch positions before touching them.
- [ ] Store the original receipt privately. A redacted copy is enough for public provenance.

If opening is allowed later, photograph the PCB revision, Zynq marking, RF-transceiver marking, DDR part numbers, QSPI part, Ethernet PHY, oscillator, baluns/filters and connector routing. An AD9361-versus-AD9363 marking is a profile-defining difference.

## 2. First power, one connection at a time

- [ ] Terminate TX1 and TX2 into rated 50-ohm loads. Leave RX terminated or disconnected; do not attach the supplied antenna yet.
- [ ] Select the documented factory boot-switch position. If no documentation is present, stop and request it rather than guessing.
- [ ] Connect only the presumed data/power port through the USB meter or a protected hub.
- [ ] Record inrush, steady current, LED sequence, heat, disconnect/reconnect behavior and boot time.
- [ ] If current is abnormal, the enclosure heats rapidly, or USB repeatedly resets, disconnect power.
- [ ] Repeat enumeration with the second USB port only after the first mapping is recorded. Do not power both ports together unless the vendor documents that configuration.

## 3. Read-only host capture

With the repository at the version used for the test:

```sh
./scripts/capture_unit.sh --output evidence/captures/UNIT_ID-usb
```

The script records whichever of `lsusb`, `system_profiler`, and `iio_info` are installed. It does not stream I/Q or write an IIO attribute. Inspect `run-metadata.txt`, the raw logs, and `SHA256SUMS`.

Record separately:

- [ ] Physical port and cable used for each capture.
- [ ] USB VID:PID, device revision, product/manufacturer/serial strings and negotiated speed.
- [ ] Configuration/interface/endpoint tree and maximum-power declaration.
- [ ] Every new block, network and serial device created by the host.
- [ ] Second USB bridge VID:PID and driver, if present.
- [ ] Whether the serial value is stable across power cycles.

Do not mount a newly presented mass-storage volume read-write. Disable desktop auto-update behavior if the volume contains a firmware-drop mechanism.

## 4. IIO and network identity

After the USB network or physical Ethernet path is understood, use discovery first:

```sh
iio_info -s
./scripts/capture_unit.sh \
  --output evidence/captures/UNIT_ID-iio \
  --iio-uri ip:pluto.local
```

- [ ] Save the context description and every context attribute.
- [ ] Record IIO device names, scan indices, channel order, formats and available attributes.
- [ ] Confirm whether two RX and two TX channels are actually exposed.
- [ ] Record IP addresses, MTU and whether the context is USB-native, USB-network, or physical Ethernet.
- [ ] Do not use `iio_attr -w`, `iio_reg`, `iio_readdev`, or `iio_writedev` in the arrival pass.

The retail “12 MSPS host” claim does not say which interface, number of channels or sample representation it covers. Preserve those as open fields.

## 5. Optional read-only SSH capture

SSH is opt-in because authentication and host-key trust require an operator decision. With a pre-established host key and SSH key:

```sh
./scripts/capture_unit.sh \
  --output evidence/captures/UNIT_ID-ssh \
  --iio-uri ip:pluto.local \
  --ssh-host root@pluto.local
```

For a first connection, `--accept-new-host-key` stores the key inside the capture directory instead of the user’s global `known_hosts`. `--interactive-ssh` permits a password prompt but never records a password. Review the fingerprint out of band when vendor material provides one.

The fixed remote program reads kernel, CPU, memory, device-tree, partition, mount, network, IIO and boot-file hash metadata. It does not run `fw_setenv`, `flashcp`, `dd`, `dfu-util`, a package manager, or any IIO write/stream command.

- [ ] Confirm reported DDR against both listing values (512 MB and 1 GB).
- [ ] Confirm the CPU clock: the storefront says 766 MHz while the pinned public XSA configures 666.666687 MHz.
- [ ] Save kernel, device-tree compatible strings and boot arguments.
- [ ] Save hashes of ordinary boot files that are readable through the filesystem.
- [ ] Record QSPI/MTD layout without reading out flash contents.
- [ ] Record FPGA/IIO core compatible strings and addresses.
- [ ] Record firmware and libiio versions.

## 6. Freeze the baseline

- [ ] Make the capture directory read-only in the archival copy.
- [ ] Copy it to a second storage location.
- [ ] Verify `SHA256SUMS` on both copies.
- [ ] Record the repository commit or archive hash used to capture it.
- [ ] Compare the new descriptor and IIO facts with the reference model; keep raw and normalized diffs.
- [ ] Fork a new board profile if PCB, RF chip, DDR, USB, firmware or channel topology differs.
- [ ] Send the vendor-material request before any update or flash action.

## 7. RX-only electrical acceptance

This stage requires proper RF equipment and is separate from arrival capture. Follow [WIDEBAND_50MHZ.md](WIDEBAND_50MHZ.md).

- [ ] Use a calibrated 50-ohm signal generator through fixed attenuation.
- [ ] Start at a low level such as -60 dBm at the connector and verify the complete loss budget.
- [ ] Keep TX disabled in both software and cabling.
- [ ] Test RX1 and RX2 independently at a narrow bandwidth before a wideband sweep.
- [ ] Check tuning, gain direction, clipping, lane order and channel isolation.
- [ ] Stop if either input behaves unexpectedly; do not compensate by raising generator power blindly.

## 8. Wideband and transport acceptance

- [ ] Set the internal sample rate to 61.44 MSPS and request a 50 MHz RX bandwidth only after narrowband operation is stable.
- [ ] Perform a conducted multitone or swept-tone test across the entire ±25 MHz baseband.
- [ ] Record passband ripple, aliases/images, SNR/EVM, phase, clipping and temperature.
- [ ] Verify sample continuity and overflow counters for bounded captures.
- [ ] Measure USB-native, USB-network and Gigabit Ethernet separately for one and two channels.
- [ ] Record sustainable duration and burst depth; do not report the configured sample rate as achieved throughput.

## 9. TX remains a separate authorization gate

Do not begin TX merely because RX passes. A TX test requires a rated dummy load or analyzer, sufficient attenuation, an independently calculated maximum power, correct SMA mapping, and compliance with local radio rules. Never cable TX directly to RX.

## Minimum acceptance record

| Question | Required answer before “unit-exact” work |
| --- | --- |
| Which PCB/enclosure revision arrived? | Photo/marking or explicitly “unknown—sealed” |
| AD9361 or another AD936x? | Marking plus software identity; discrepancies retained |
| 512 MB or 1 GB DDR? | Runtime and device-tree evidence, ideally part marking |
| 766 or about 666.67 MHz CPU? | Boot clock summary/runtime evidence; retain both source values |
| Which connector is OTG/data/power/UART? | Per-port enumeration and physical photo |
| Which USB personality and descriptors? | Raw high-speed descriptor capture and SHA-256 |
| Which firmware/bitstream/device tree? | Versions and file hashes; no assumed stock Pluto identity |
| Are two RX/TX lanes present and ordered correctly? | IIO schema plus later conducted lane test |
| Does 50 MHz work on the board? | E5 conducted sweep, not just an accepted attribute write |
| Can raw wideband samples reach the host? | Measured loss-free rate by interface/channel count/format |
