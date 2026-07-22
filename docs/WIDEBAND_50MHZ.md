# The 50 MHz bandwidth requirement

## Bottom line

The advertised AD9361 can be configured for up to 56 MHz channel bandwidth, so 50 MHz is a credible on-chip analog target. The P210 listing does not establish that 50 MHz of flat, low-alias board-level bandwidth is delivered, nor that raw 50 MHz-wide 2x2 IQ can stream continuously to a host.

Those are three separate contracts:

1. RF contract: the SMA-to-AD9361 path preserves the required 50 MHz signal band.
2. Internal sample contract: AD9361, FPGA and DMA sustain 61.44 MSPS with correct lanes and no silent loss.
3. Host transport contract: USB or Ethernet moves the chosen channels and format without overflow for the required duration.

Only the first two need to run at full rate if FPGA processing reduces data before it crosses the host interface.

## Rate arithmetic

Native IIO commonly carries each 12-bit converter component in a signed 16-bit little-endian container. One complex sample is therefore 4 bytes per channel. For two synchronized channels, one sample-time frame is 8 bytes.

Keep four different units separate:

- one byte is 8 bits;
- the external DDR bus/transfer word is 16 bits, or 2 bytes per beat;
- each I or Q component has 12 significant converter bits stored in a 16-bit container; and
- one complex sample has I plus Q, so it is 4 bytes for each enabled channel.

The pinned public XSA exposes four 16-bit RX packer lanes—RX1-I, RX1-Q, RX2-I, RX2-Q—as one 64-bit RX DMA word. That 8-byte word is one simultaneous two-channel sample-time frame. The external DDR bus can still be 16 bits because a DMA word is transferred over multiple memory beats. DDR bus width, DMA width, converter precision and host container size are distinct quantities.

| Complex sample rate | One channel payload | Two-channel payload |
| ---: | ---: | ---: |
| 12 MSPS | 48 MB/s (384 Mb/s) | 96 MB/s (768 Mb/s) |
| 50 MSPS | 200 MB/s (1.600 Gb/s) | 400 MB/s (3.200 Gb/s) |
| 61.44 MSPS | 245.76 MB/s (1.966 Gb/s) | 491.52 MB/s (3.932 Gb/s) |

Compare those payloads with ceilings, not expected application performance:

- USB 2.0 high-speed signals at 480 Mb/s, or 60 MB/s before transaction, scheduling, controller and software overhead.
- 1000BASE-T signals at 1 Gb/s, or 125 MB/s before Ethernet, IP, TCP, IIOD and host overhead.
- The retail listing says “P210-12MSPS” with host and “61.44MSPS” burst, but does not define interface, channel count, sample format, loss criterion or burst duration.

Consequences:

- Raw 50/61.44 MSPS cannot continuously cross either listed host interface even for one native 16-bit I/Q channel.
- Two channels at the listed 12 MSPS consume 96 MB/s. That is below the Gigabit Ethernet line-rate ceiling but leaves limited protocol/software margin; it is above the USB 2.0 signaling ceiling.
- The 12 MSPS claim is modeled conservatively as a one-channel E0 budgeting basis until measured.
- At 61.44 MSPS, a two-channel stream needs at least 4:1 rate reduction even against ideal Gigabit Ethernet and 9:1 against ideal USB 2.0. Real systems need more margin.

Run the executable budget:

```sh
neptune-twin wideband
```

## Burst depth

If all advertised 512 MiB of DDR were available solely for samples, which it is not, a 61.44 MSPS native stream would have these absolute upper bounds:

- two channels: about 1.09 seconds;
- one channel: about 2.18 seconds.

Linux, the FPGA design, reserved memory, DMA allocation, alignment and buffering reduce the actual duration—often substantially. The listing also contradicts itself by mentioning 1 GB once. Treat both memory capacity and burst depth as delivered-unit measurements.

## A workable architecture

For continuous 50 MHz observation, keep the wide sample path on the FPGA side and move a reduced product to the host:

- polyphase channelizer or selected digital downconverters;
- decimation after a matched anti-alias filter;
- FPGA FFT/power spectra instead of time-domain IQ;
- trigger and pre/post-trigger burst buffers;
- event or feature extraction;
- lossless compression only when a measured signal distribution makes its worst-case behavior acceptable.

### Default spectrum contract

The project makes the FPGA-spectrum option executable at both the
firmware-visible block/MMIO contract and the continuous reference-PL contact:

```sh
neptune-twin fft-plan
```

The default architecture budget is a 65,536-point FFT on both channels at
61.44 MSPS with 65,536 selected bins per channel. Bin spacing is 937.5 Hz.
The continuous runtime block-averages consecutive frames to an effective update rate just
under 20 Hz, and each power bin is encoded as an unsigned 16-bit log-power
value. The complete wire contract is `NSFT` version 1 in network byte order
with sequence/configuration/timestamp/loss fields and CRC32. The ARM runtime
intentionally proves the firmware-visible path one block at a time. The
continuous reference runtime owns the RF source, detects any sample-index or
mixed-epoch discontinuity, never mixes a retune into one result, and stalls at
bounded egress capacity instead of silently dropping an update. Its snapshot
reports compute lag explicitly; the requested update rate is a sample-time
ceiling, not a claim of Python wall-clock throughput.

`SpectrumTCPPublisher` sends complete NSFT updates as one self-framing TCP byte stream; `SpectrumStreamDecoder` accepts arbitrary TCP chunks, enforces the declared maximum length, restores packet boundaries, and validates CRC32. The virtual appliance exposes that stream on its dedicated spectrum TCP contact; the physical target can carry it over Gigabit Ethernet. The current narrow USB-RNDIS model proxies IIOD on TCP/30431 only, so NSFT-over-RNDIS is a future deployment option rather than a tested virtual contact. TCP avoids inventing an application fragmentation protocol: a full 65,536-bin float32 spectrum packet exceeds one UDP datagram.

Full two-channel spectra use 262,144 payload bytes per update and about 5.24 MB/s at 20 updates/s, plus small per-channel packet headers and CRCs. That fits the model’s conservative 48 MB/s host payload budget with margin. Selecting fewer bins reduces egress linearly.

The input budget assumes two aggregate complex-sample lanes at a 100 MHz stream clock, enough arithmetically for 2 × 61.44 MSPS. It is not a synthesis result. FFT IP architecture, two-channel scheduling, BRAM/DSP usage, clock-domain crossings, fixed-point growth, FIFO depth and post-route timing must be closed in the actual XC7Z020 project before this becomes an implementation claim.

### Executable pre-arrival hardware path

The P210-enabled QEMU machine now exercises the intended software-visible
path with real ARM firmware components. The ARM source, input locks, generated
runtime manifest, and [canonical FFT ABI](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave/blob/main/docs/P210_FFT_ABI.md)
are owned by the pinned
[`Atom-NeptuneSDR_Firmwave`](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave)
checkout; this Twin owns and verifies the QEMU/contact side:

```text
AD9361/CF-AXI -> four-entry AXI-DMAC -> Linux RX IIO buffer
  -> ARM block copy -> reserved DDR -> P210 FFT MMIO/DMA ABI
  -> ARM NSFT packetizer -> GEM/TCP -> host decoder
```

The tested runtime sets the RX sampling frequency to 61.44 MSPS and the shared
RX RF-bandwidth attribute to 50 MHz, captures one 65,536-frame time-major
two-channel IQ16 block, copies it into the FFT input window, runs a deterministic
two-channel 65,536-point integer FFT, and
transmits 262,288 bytes for the two NSFT packets.  The host validates packet
length/framing/CRC, encoding, cross-channel update metadata, and the expected
deterministic tone bins. The retained evidence contains exactly those two raw
packets, not an arbitrary TCP receive chunk. In the same boot, official host
libiio independently interrogates the released ARM `iiod` control service.

Before and after each block, ARM reads the live RX LO, sample rate, and RF
bandwidth from the AD9361 IIO device. A mid-capture change discards the mixed
block; a stable changed profile advances `config_epoch`, and both channel
packets carry the same captured timestamp, epoch, center frequency, and sample
rate. The spectrum TCP service has one active client and a two-second absolute
send deadline, so a non-reading client cannot hold the data plane forever.

That closes the block-capture/firmware/MMIO/FFT-DMA/packing/transport contact
in simulation. The composed reference-PL layer closes the continuous
sample-order/averaging/backpressure semantics at the same output contact; it
does not turn the 50 MHz attribute write into an RF measurement or prove that
the seller bitstream contains this pipeline. The guest stops and restarts its IIO buffer for each block. The
fixed `/dev/mem` windows are safe in this QEMU launch because Linux is limited
to 384 MiB of 512 MiB, but they are not a physical deployment design. Hardware
needs device-tree reserved memory plus a kernel driver with DMA allocation and
non-coherent Zynq cache synchronization, or a direct PL stream that avoids the
CPU copy. The emulator
has no SMA matching network or analog impairments, and the FFT device is a
functional QEMU implementation of the proposed ABI rather than synthesized
XC7Z020 RTL.  The conducted E5 procedure below is still required on arrival.

If the requirement is continuous, undecimated, two-channel, 50 MHz time-domain IQ at the host, the listed USB 2.0 and Gigabit Ethernet interfaces are the wrong transports. That requirement implies a faster interface or a different board architecture.

## Define “50 MHz works” before testing

An accepted `rf_bandwidth=50000000` write is not a bandwidth measurement. Freeze an acceptance profile first:

| Parameter | Required declaration |
| --- | --- |
| Center-frequency grid | Frequencies over which the board must pass |
| Complex sample rate | 61.44 MSPS recommended for this profile |
| Occupied baseband | At least -25 MHz to +25 MHz, with declared edge guard |
| Channels/mode | RX1, RX2, simultaneous 2R2T/FDD or another explicit mode |
| Passband ripple/droop | Numeric limit chosen for the application |
| Alias/image rejection | Numeric limit and where it is evaluated |
| SNR, EVM or noise figure | Numeric limit, signal type, level and gain mode |
| Phase/amplitude match | Numeric MIMO limit and calibration policy |
| Loss criterion | Zero unreported gaps; counter/sequence policy for reported gaps |
| Duration | Continuous interval or exact burst length |
| Temperature/power | Declared envelope and warm-up time |

The AD9361 data sheet establishes a chip envelope; external baluns, matching, layout, clock, supplies and the selected FIR/decimation chain determine board performance.

## Safe conducted RX test

Use a shielded, 50-ohm bench path. Do not use over-the-air transmission for acceptance.

Recommended topology:

```text
calibrated signal generator -> fixed attenuator(s) -> splitter if needed -> RX input(s)
                                      |
                                      +-> calibrated power meter/analyzer check
```

Procedure:

1. Keep both TX paths disabled and terminated.
2. Verify cable, splitter and attenuator loss across the entire RF frequency range being tested.
3. Start around -60 dBm at the board connector. Confirm gain and clipping behavior before increasing level.
4. Stay comfortably below the project contract’s conservative -10 dBm ceiling; until board protection is known, a -30 dBm or lower working level provides useful margin for most linear tests.
5. Begin with a narrowband tone near baseband center on RX1, then RX2.
6. Sweep offset across ±25 MHz, or use a calibrated multitone/OFDM waveform with controlled crest factor.
7. Repeat across center frequency, gain mode and temperature. Capture raw data, context, filter configuration, overflow counters and equipment settings.
8. Test both inputs independently, then simultaneously to measure isolation and lane mapping.

Do not connect a signal source that can present DC unless the board documentation permits it. Use a suitable DC block where required by the source/board combination.

## TX and loopback safety

TX validation is not part of arrival capture. When separately authorized:

- terminate into a rated dummy load or spectrum analyzer path;
- insert enough fixed attenuation for the maximum possible board output, not merely the requested attenuation setting;
- verify power with an independent instrument before connecting any RX input;
- never directly connect TX SMA to RX SMA;
- keep a physical attenuator in a conducted loopback path and account for its frequency response;
- do not attach an antenna or radiate unless the frequency, power, emission and operator authorization are lawful at the test location.

## Evidence needed for closure

An E5 result should archive the board ID/revision, firmware and bitstream hashes, descriptor/IIO schema, sample format, exact filter settings, generator/analyzer calibration dates, cable/attenuator S-parameters or loss data, center-frequency and offset grid, temperatures, raw IQ, sequence/loss counters, analysis code revision, uncertainty and pass/fail thresholds.

Sources for the chip limits and transport interpretation are listed in [EVIDENCE.md](EVIDENCE.md).
