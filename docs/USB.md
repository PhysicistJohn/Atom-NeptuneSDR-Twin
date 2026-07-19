# USB behavior, capture and gadget deployment

## Physical interfaces are revision-dependent

The retail listing names one USB 2.0 OTG port and one integrated USB-UART/JTAG interface, both using USB-C on the described product. It does not identify connector orientation, bridge chip, power path or exact descriptors.

A public report for another Neptune P210 revision observed:

- the connector next to Ethernet as the powered OTG/composite-device port; and
- the connector next to the boot switches as a CH341 UART (`1a86:7523`).

That report also found a different RF-chip marking than the current listing. It is evidence that revisions vary, not a wiring instruction for this unit. Capture each physical connector separately and label it by a photo.

## Byte-locked reference personality

`src/neptunesdr_twin/data/usb-p210-observed.json` contains a reference high-speed descriptor tree. Its deterministic fields are locked by tests.

| Field | Reference value |
| --- | --- |
| VID:PID | `0456:b673` |
| USB / device BCD | `0x0200` / `0x0515` |
| Manufacturer | `Analog Devices Inc.` |
| Product | `PlutoSDR (ADALM-PLUTO)` |
| Configuration | 218 bytes, six interfaces, bus powered, 500 mA declaration |
| Interfaces 0–1 | RNDIS control/data |
| Interface 2 | USB mass storage |
| Interfaces 3–4 | CDC ACM control/data |
| Interface 5 | Native libiio FunctionFS, three endpoint pairs |
| Native IIO endpoint pairs | `86/04`, `87/05`, `88/06` |

The fixture deliberately preserves allocated empty string descriptors at indices 8, 10 and 14. Its device and configuration raw bytes are parsed and SHA-256 locked in the USB tests.

The fixture also records a DFU metadata personality at `0456:b674`, with five named alternate settings and a 4096-byte transfer size. The twin does not treat DFU metadata as permission to write flash, and the arrival script never enters DFU.

None of those values is a delivered-unit fact until a fresh descriptor capture agrees.

## Firmware and virtual-device composition

The public P210 device tree configures its enabled ChipIdea controller with
`dr_mode = "host"`, while the official Pluto v0.39 `/etc/init.d/S23udc`
service expects a gadget UDC.  Upstream QEMU 10.0.2 supplies the Zynq
host-controller side, not a P210 device controller/PHY.  The twin preserves
that fact instead of changing the public device tree to make boot logs look
clean.

The complete virtual appliance closes the host-facing contact with a standard
USB/IP transport adapter.  It exports the byte-locked six-interface device,
routes EP0 through the same control-endpoint state machine, and implements the
bulk/class behavior outside the guest's absent UDC.  The native-IIO endpoint
can either use the local deterministic IIO context or bridge its IIOD byte
stream unchanged to the released `iiod` running inside the P210 QEMU guest.
This split is intentional:

- QEMU remains the firmware/driver execution layer and is not falsely claimed
  to have a device-mode controller;
- USB/IP supplies a device visible to a normal Linux VHCI/USB stack;
- the guest bridge makes USB-native libiio and Ethernet libiio reach the same
  released ARM service; and
- descriptor identity remains a reference-profile claim until the purchased
  revision is captured.

Run the firmware guest in one terminal and the bridge in another. USB/IP has
no authentication or encryption, so the remote-binding example must be
firewalled to a trusted development LAN; use `--host 127.0.0.1` for a local
Linux client.

```sh
scripts/run_p210_firmware.sh --serve
neptune-twin usbip-serve --host 0.0.0.0 \
  --iiod-backend 127.0.0.1:30431
```

Attach from Linux with its standard USB/IP client:

```sh
sudo modprobe vhci-hcd
usbip list -r TWIN_HOST
sudo usbip attach -r TWIN_HOST -b 1-1
```

USB 2.0 still cannot continuously carry the raw 2x2 61.44-MSPS stream. That
is a bandwidth limit, not a missing USB implementation. Full-rate work is
reduced on chip; the virtual NSFT result uses the separate spectrum TCP
contact, while the narrow RNDIS model intentionally carries IIOD only.

## What the executable USB model covers

The executable USB model provides:

- strict device/configuration/string descriptor parsing and validation;
- byte-exact reference descriptor replies with host `wLength` truncation;
- USB configuration state;
- USB/IP 1.1.1 device-list, import, submit and unlink framing;
- native-IIO reset/open/close pipe requests, three independent bulk sessions,
  bounded transfers and optional released-guest IIOD bridging;
- bus reset behavior;
- a deterministic read-only FAT12 mass-storage target and SCSI bulk-only
  transport;
- CDC ACM line/control state and console bulk queues;
- RNDIS control/OID messages, bounded Ethernet queues, DHCP for a
  `192.168.2.10` host lease, ARP and ICMP echo at `192.168.2.1`, and an
  in-order TCP/30431 proxy to the same local or released-guest IIOD backend;
- normal and DFU identity metadata; and
- deterministic MAC derivation compatible with the modeled firmware rule.

It creates a virtual USB peripheral, not electrical signaling on the Mac's USB
pins. A Linux USB/IP client supplies the ordinary host-controller/device-core
path and creates host class devices from the exported interfaces. RNDIS is a
narrow Pluto-style management network, not a general router: it implements the
DHCP/ARP/ICMP and single in-order IIOD TCP session needed by
`ip:192.168.2.1`; unsupported ports are reset and IPv6, fragmentation and a
general retransmission/timer engine are deliberately absent. USB/IP itself is
reliable and ordered. The mass-storage function is intentionally read-only,
and DFU download is intentionally absent.

Inspect the fixture:

```sh
neptune-twin usb
python -m unittest discover -s tests -v
```

## Host-facing access paths

ADI’s Pluto stack normally offers four related paths:

- native libiio USB backend, addressed with a `usb:` URI;
- network libiio through USB RNDIS/ECM, commonly `ip:pluto.local`;
- CDC ACM serial console; and
- a small mass-storage/update volume.

For the USB/IP twin, the RNDIS host lease is `192.168.2.10/24` and released or
local IIOD is reachable at `ip:192.168.2.1`. Native USB IIO reaches that same
backend through interface 5 without IP. The separate NSFT spectrum contact is
not tunneled through the current RNDIS model.

The P210 additionally advertises physical Gigabit Ethernet and a separate USB-UART port. Do not collapse those into one throughput number.

Safe discovery commands are:

```sh
iio_info -s
iio_info -u usb:
iio_info -u ip:pluto.local
```

`iio_info` reads the context surface. The arrival workflow intentionally excludes `iio_attr -w`, `iio_reg`, `iio_readdev` and `iio_writedev` because they write state or stream RF data.

On Linux, `lsusb -v -d 0456:b673` captures the descriptor tree. On macOS, `system_profiler SPUSBDataType -detailLevel full` captures the I/O Registry’s USB view. `scripts/capture_unit.sh` uses whichever tools are installed and records failures rather than hiding them.

## Linux gadget dry run

`scripts/linux_usb_gadget.sh` prepares a configfs composite plan. Without `--apply`, it only prints actions and is safe to run on any host:

```sh
./scripts/linux_usb_gadget.sh

./scripts/linux_usb_gadget.sh \
  --mass-storage-image /absolute/path/to/read-only.img \
  --functionfs-mount /dev/ffs-iio
```

An applied skeleton requires Linux with configfs, libcomposite and a free USB Device Controller. Binding affects the machine’s physical USB device port, so apply is deliberately gated:

```sh
sudo ./scripts/linux_usb_gadget.sh \
  --apply \
  --acknowledge-observed-vid \
  --serial P210TWIN-UNIQUE-001 \
  --udc YOUR_UDC_NAME
```

Important limits:

- `0x0456` is Analog Devices’ VID. Do not ship or publicly identify a product with it without authorization. The acknowledgment flag exists to prevent accidental use, not to grant rights.
- The default applied skeleton includes RNDIS and ACM. Mass storage is added only with an explicit backing file and is forced read-only.
- Native IIO is added only with an explicit FunctionFS mount. A userspace FunctionFS implementation must write descriptors/strings and report `ready=1` before the script will bind.
- The kernel and UDC assign interface IDs/endpoints and emit class descriptors. A generic configfs construction is not guaranteed to match the 218 reference bytes.
- The script refuses to overwrite an existing gadget and has no automatic teardown path. Inspect and unbind deliberately on the gadget machine.
- An ACM tty and RNDIS link are transport shells; they do not make the Python twin’s IIOD TCP endpoint available automatically. Network setup/routing and native FunctionFS service are separate integration work.
- A future physical gadget integration may route the reduced NSFT spectrum stream as ordinary TCP over RNDIS after IP configuration. The current USB/IP RNDIS model does not: it intentionally exposes IIOD on TCP/30431 only. NSFT remains distinct from both IIOD and the native libiio FunctionFS interface.

FunctionFS preparation and binding are deliberately separate so the gadget cannot enumerate before userspace has supplied descriptors:

```sh
sudo ./scripts/linux_usb_gadget.sh \
  --apply --acknowledge-observed-vid \
  --serial P210TWIN-UNIQUE-001 \
  --functionfs-mount /dev/ffs-iio \
  --udc YOUR_UDC_NAME

# Start the separate userspace FunctionFS IIO service here and verify ready=1.

sudo ./scripts/linux_usb_gadget.sh \
  --apply --bind-existing --acknowledge-observed-vid \
  --name neptune_p210_twin \
  --functionfs-mount /dev/ffs-iio \
  --udc YOUR_UDC_NAME
```

## Exact USB acceptance

For E4 differential evidence, compare at least:

- device, qualifier if present, all configurations, BOS if present, and every string/language descriptor;
- interface association, interface order, alternate settings and endpoint order;
- endpoint address, transfer type, maximum packet size and interval at each negotiated speed;
- class-specific descriptors and Microsoft OS descriptors;
- configuration selection, reset, suspend/resume and disconnect/reconnect traces;
- native-IIO vendor requests, valid/invalid pipe numbers, stalls, short packets and zero-length packets;
- serial and MAC stability rules; and
- normal, recovery and DFU personalities without performing a download.

Normalize host bus/address numbers and timestamps only. Do not normalize descriptor bytes, interface order, stalls or packet termination semantics simply because they differ.

## Throughput boundary

USB 2.0 high-speed has a 480 Mb/s signaling ceiling, not a 480 Mb/s application guarantee. One 12 MSPS native 16-bit I/Q channel is already 384 Mb/s of payload; two are 768 Mb/s. Full 50 MHz raw capture cannot cross this link continuously. See [WIDEBAND_50MHZ.md](WIDEBAND_50MHZ.md).
