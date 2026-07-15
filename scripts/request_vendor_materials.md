Subject: HAMGEEK P210 SKU 95157 — source, hardware revision, and 50 MHz validation materials

Hello,

I purchased a HAMGEEK P210 SDR development board with enclosure, SKU 95157, advertised as XC7Z020 + AD9361, 2T2R, 70 MHz–6 GHz, up to 56 MHz signal bandwidth, 12 MSPS host transfer, and 61.44 MSPS burst sampling.

I want to preserve the factory image and build a reproducible development/test environment before changing the device. Please provide the material for the exact shipped hardware revision, preferably through a versioned download with SHA-256 hashes:

1. Hardware identity
   - PCB and enclosure revision, serial/revision interpretation, and revision history.
   - Schematic, BOM with fitted options, PCB connector map, expansion/JTAG pinout, and mechanical drawing/STEP file.
   - Exact Zynq, RF transceiver, DDR, QSPI, Ethernet PHY, USB-UART/JTAG bridge, TCXO, balun/filter and power-tree parts fitted to this revision.
   - Boot-mode switch table and the documented function/power behavior of each USB-C connector.

2. FPGA project
   - Complete synthesizable HDL/block design, constraints, IP configuration, scripts, and generated-source policy.
   - Vivado/Vitis version and patches, project recreation command, build instructions, and expected build hashes or reports.
   - Matching XSA/HDF, FSBL, bitstream, register map, AXI core versions, DMA/sample format, channel/lane order, clocking, interrupt map, and DDR buffer layout.
   - Source and configuration for ZED-FMCOMMS2/3, PlutoSDR, openwifi, and any P210-specific branches referenced by the listing.

3. Firmware/software
   - Factory QSPI image and SD-card image for this revision, plus a non-destructive backup procedure and documented recovery procedure.
   - U-Boot/FSBL sources and configuration, Linux source/commit and patches, device-tree source, Buildroot/rootfs configuration, libiio/iiod sources, startup scripts, and all P210-specific applications.
   - Toolchain versions, reproducible build instructions, dependency/submodule commits, license notices, and corresponding source for redistributed GPL/LGPL components.
   - Partition table, boot environment defaults, expected file/image sizes and SHA-256 hashes.
   - Resolution of the CPU-clock conflict: the product page says 766 MHz, while the pinned public P210 XSA configures 666.666687 MHz.

4. USB and host interfaces
   - Expected VID:PID values for normal/recovery/DFU modes, full descriptor dumps at each speed, string/serial/MAC derivation, and the second USB bridge identity.
   - Source/configuration for RNDIS/ECM, mass-storage, CDC ACM, native libiio FunctionFS, DFU, and any USB-JTAG function.
   - Supported libiio, MATLAB, GNU Radio and host-driver versions for Linux, macOS, and Windows.

5. RF and 50 MHz performance
   - Confirmation that the shipped component is AD9361 (not AD9363 or another AD936x) and that both RX and both TX paths are fitted and supported.
   - Factory calibration data/format and production test procedure.
   - Schematics and frequency response for each SMA-to-chip path, including safe RX input limits and maximum TX output by frequency.
   - Measured results for a 50 MHz occupied signal bandwidth: sample rate, FIR/filter configuration, center-frequency grid, passband ripple, alias/image rejection, SNR/EVM, channel isolation, phase/amplitude matching, and temperature conditions.
   - TCXO nominal frequency, 0.5 ppm test basis, correction procedure, and any per-unit calibration value.

6. Throughput definitions
   - For “P210-12MSPS” host transfer: whether the rate is per channel or aggregate; RX or TX/full duplex; USB-native, USB-network, or Gigabit Ethernet; sample format; buffer size; host/software; test duration; and permitted loss/overflow.
   - For “61.44MSPS burst sampling”: channel count, direction, DDR capacity available to DMA, maximum burst length, trigger behavior, sample format, transfer/readout time, and overflow indication.
   - Resolution of the product-page DDR conflict: the page states both 512 MB and 1 GB, while “Hardware Configuration” states 512 MB 16-bit DDR3.

Please identify any files that are generic PlutoSDR upstream files versus P210-specific modifications, and provide the exact commits used for the shipped image. If some design files are not public, please state that explicitly and provide at least the binary hashes, interfaces/register maps, recovery image, licenses, and production acceptance limits needed to use the board safely.

Thank you.
