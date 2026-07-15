# Pinned host libiio workflow

The host client is Analog Devices `libiio` **v0.26**, commit
`a0eca0d2bf10326506fb762f0eec14255b27bef5`. That is the libiio release in
the official PlutoSDR v0.39 userspace used by the executable runtime. The
build verifies both that commit and its Git tree before compiling.

Build it without installing anything into the host system:

```sh
scripts/build_host_libiio.sh
scripts/build_host_libiio.sh --verify
```

Source, build output, and the installation all remain below
`.cache/host-libiio-v0.26/`. A native compiler, Git, CMake, pthreads, and
libxml2 headers/libraries must already be available. The script uses the
project `.venv` CMake and Ninja when present. It disables USB, serial,
ZeroConf, local-IIO, and server support; this particular build is the host
TCP/XML client, not a USB implementation and not the guest `iiod`.

When QEMU forwards guest TCP port 30431 to host port 30431, inspect the real
guest context with:

```sh
scripts/host_iio.sh info
```

The equivalent explicit URI is `ip:127.0.0.1:30431`. Override it with
`--uri` or `NEPTUNE_IIO_URI`.

Once the guest exposes a scan-capable RX device, capture 65,536 complete
scan frames with the upstream `iio_readdev` binary:

```sh
scripts/host_iio.sh read -b 65536 -s 65536 cf-ad9361-lpc > rx.iq16le
```

`iio_readdev` writes only binary scan data to standard output. The byte
layout is defined by the context XML returned by the running guest. For a
four-channel I0/Q0/I1/Q1 scan with 16-bit containers, one complete frame is
8 bytes, so the example is 524,288 bytes. That layout must be checked from
the guest context rather than inferred from the DDR3 bus width.

This workflow proves the host library and the IIOD TCP contract only after a
connection succeeds and the guest reports its IIO devices. It does not by
itself prove USB FunctionFS, FPGA timing, AD9361 RF behavior, or DMA data
correctness.
