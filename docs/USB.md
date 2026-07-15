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

## Firmware-executing QEMU status

The ARM/QEMU runtime does **not** currently claim a working USB-device port.
The public P210 device tree configures its enabled ChipIdea controller with
`dr_mode = "host"`, while the official Pluto v0.39 `/etc/init.d/S23udc`
service expects a gadget UDC so it can create RNDIS, mass-storage, ACM, and
FunctionFS IIO functions.  Those contacts contradict each other.  Upstream
QEMU 10.0.2's Zynq ChipIdea model also supplies the host-controller side, not
the missing P210 device-controller/PHY behavior needed to bind that gadget.

Consequently, an unmodified full init reaches Linux but its USB gadget setup
fails explicitly; the hardware-test init bypasses that service and uses the
board's advertised Gigabit Ethernet contact.  The real ARM `iiod` network
service and the ARM FFT spectrum service have both been exercised through
QEMU's GEM and host-forwarded TCP.  This is the appropriate pre-arrival path
for full spectra anyway: USB 2.0 cannot continuously carry the raw 2x2
61.44-MSPS stream.

This boundary must remain visible:

- byte-locked descriptor/EP0 behavior is E2 model evidence;
- the ARM Linux AD9361/IIO/DMAC/Ethernet path is E3 integration evidence;
- ARM FunctionFS plus an emulated UDC and actual host enumeration are not yet
  E3; and
- the purchased unit's two physical USB connectors still require arrival
  capture before any revision-specific claim.

Closing the QEMU USB gap requires a derived peripheral-mode device tree and a
ChipIdea UDC/PHY implementation (or an equivalent USB-device transport) that
runs the released FunctionFS service.  Relabeling the controller or presenting
host-side descriptor bytes without that firmware path would not close it.

## What the executable USB model covers

The Python model provides:

- strict device/configuration/string descriptor parsing and validation;
- byte-exact reference descriptor replies with host `wLength` truncation;
- USB configuration state;
- native-IIO reset/open/close pipe requests and pipe state;
- bus reset behavior;
- normal and DFU identity metadata; and
- deterministic MAC derivation compatible with the modeled firmware rule.

It does not by itself create a physical USB peripheral, run a mass-storage filesystem, implement RNDIS packets, expose a real CDC tty, or serve native IIO bulk endpoints through a host controller. Those require a Linux USB Device Controller, kernel gadget functions and userspace services.

Inspect the fixture:

```sh
neptune-twin usb
python -m unittest tests.test_usb -v
```

## Host-facing access paths

ADI’s Pluto stack normally offers four related paths:

- native libiio USB backend, addressed with a `usb:` URI;
- network libiio through USB RNDIS/ECM, commonly `ip:pluto.local`;
- CDC ACM serial console; and
- a small mass-storage/update volume.

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
- The reduced NSFT spectrum stream may run as ordinary TCP over RNDIS after IP configuration. That is distinct from both IIOD and the native libiio FunctionFS interface.

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
