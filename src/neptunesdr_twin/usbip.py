"""Host-enumerable USB/IP transport for the NeptuneSDR composite gadget.

The reference USB model in :mod:`neptunesdr_twin.usb` owns descriptor and
endpoint-zero semantics.  This module puts that model behind Linux's standard
USB/IP protocol and supplies bounded data-plane models for every advertised
function:

* RNDIS control plus a deterministic 192.168.2.1 management network (DHCP,
  ARP, ICMP echo, and an in-order TCP proxy to IIOD on port 30431);
* a read-only USB mass-storage volume using Bulk-Only Transport;
* a deterministic CDC ACM console; and
* three independent native-libiio/IIOD FunctionFS sessions.

USB/IP is a transport substitution, not a claim that QEMU contains a Zynq USB
device controller.  A Linux host can import this device with ``usbip attach``
and then exercise the same USB requests and bulk contacts that firmware-facing
software uses.  Protocol fields are network byte order as specified by the
Linux kernel USB/IP documentation; USB setup packets and class payloads retain
their native USB byte order.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import errno
import ipaddress
import socket
import socketserver
import struct
import threading
from typing import Callable, Deque, Dict, Mapping, Optional, Tuple

from .errors import USBProtocolError
from .iio import IIOContext, IIODSession, MAX_IIOD_PAYLOAD_BYTES
from .usb import (
    IIO_REQ_CLOSE_PIPE,
    IIO_REQ_OPEN_PIPE,
    IIO_REQ_RESET_PIPES,
    SetupPacket,
    USBControlEndpoint,
    USB_DIR_IN,
    USB_DIR_OUT,
    USB_RECIP_DEVICE,
    USB_RECIP_INTERFACE,
    USB_TYPE_STANDARD,
)


USBIP_VERSION = 0x0111
USBIP_PORT = 3240
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
USBIP_CMD_SUBMIT = 0x00000001
USBIP_CMD_UNLINK = 0x00000002
USBIP_RET_SUBMIT = 0x00000003
USBIP_RET_UNLINK = 0x00000004
USBIP_DIR_OUT = 0
USBIP_DIR_IN = 1
USBIP_SPEED_HIGH = 3

USB_REQ_CLEAR_FEATURE = 0x01
USB_REQ_GET_STATUS = 0x00
USB_REQ_SET_FEATURE = 0x03
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_GET_INTERFACE = 0x0A
USB_REQ_SET_INTERFACE = 0x0B

USB_TYPE_CLASS = 0x20
USB_RECIP_ENDPOINT = 0x02

CDC_SEND_ENCAPSULATED_COMMAND = 0x00
CDC_GET_ENCAPSULATED_RESPONSE = 0x01
CDC_SET_LINE_CODING = 0x20
CDC_GET_LINE_CODING = 0x21
CDC_SET_CONTROL_LINE_STATE = 0x22
CDC_SEND_BREAK = 0x23

MSC_BULK_ONLY_RESET = 0xFF
MSC_GET_MAX_LUN = 0xFE

RNDIS_PACKET_MSG = 0x00000001
RNDIS_INITIALIZE_MSG = 0x00000002
RNDIS_HALT_MSG = 0x00000003
RNDIS_QUERY_MSG = 0x00000004
RNDIS_SET_MSG = 0x00000005
RNDIS_RESET_MSG = 0x00000006
RNDIS_KEEPALIVE_MSG = 0x00000008
RNDIS_INITIALIZE_CMPLT = 0x80000002
RNDIS_QUERY_CMPLT = 0x80000004
RNDIS_SET_CMPLT = 0x80000005
RNDIS_RESET_CMPLT = 0x80000006
RNDIS_KEEPALIVE_CMPLT = 0x80000008
RNDIS_STATUS_SUCCESS = 0x00000000
RNDIS_STATUS_NOT_SUPPORTED = 0xC00000BB

OID_GEN_SUPPORTED_LIST = 0x00010101
OID_GEN_HARDWARE_STATUS = 0x00010102
OID_GEN_MEDIA_SUPPORTED = 0x00010103
OID_GEN_MEDIA_IN_USE = 0x00010104
OID_GEN_MAXIMUM_LOOKAHEAD = 0x00010105
OID_GEN_MAXIMUM_FRAME_SIZE = 0x00010106
OID_GEN_LINK_SPEED = 0x00010107
OID_GEN_TRANSMIT_BLOCK_SIZE = 0x0001010A
OID_GEN_RECEIVE_BLOCK_SIZE = 0x0001010B
OID_GEN_VENDOR_ID = 0x0001010C
OID_GEN_VENDOR_DESCRIPTION = 0x0001010D
OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E
OID_GEN_CURRENT_LOOKAHEAD = 0x0001010F
OID_GEN_DRIVER_VERSION = 0x00010110
OID_GEN_MAXIMUM_TOTAL_SIZE = 0x00010111
OID_GEN_MEDIA_CONNECT_STATUS = 0x00010114
OID_GEN_PHYSICAL_MEDIUM = 0x00010202
OID_GEN_XMIT_OK = 0x00020101
OID_GEN_RCV_OK = 0x00020102
OID_GEN_XMIT_ERROR = 0x00020103
OID_GEN_RCV_ERROR = 0x00020104
OID_GEN_RCV_NO_BUFFER = 0x00020105
OID_802_3_PERMANENT_ADDRESS = 0x01010101
OID_802_3_CURRENT_ADDRESS = 0x01010102
OID_802_3_MULTICAST_LIST = 0x01010103
OID_802_3_MAXIMUM_LIST_SIZE = 0x01010104
OID_802_3_MAC_OPTIONS = 0x01010105
OID_802_3_RCV_ERROR_ALIGNMENT = 0x01020101
OID_802_3_XMIT_ONE_COLLISION = 0x01020102
OID_802_3_XMIT_MORE_COLLISIONS = 0x01020103

_OP_HEADER = struct.Struct("!HHI")
_BASIC_HEADER = struct.Struct("!IIIII")
_SUBMIT_BODY = struct.Struct("!Iiiii8s")
_RET_SUBMIT_BODY = struct.Struct("!iiiii8s")
_UNLINK_BODY = struct.Struct("!I24s")
_RET_UNLINK_BODY = struct.Struct("!i24s")
_DEVICE_FIXED = struct.Struct("!IIIHHHBBBBBB")
_INTERFACE_INFO = struct.Struct("!BBBB")
_CBW = struct.Struct("<4sIIBBB16s")
_CSW = struct.Struct("<4sIIB")

MAX_URB_BYTES = 16 * 1024 * 1024
MAX_IIOD_LINE = 64 * 1024
MAX_IIOD_BUFFERED_BYTES = MAX_IIOD_PAYLOAD_BYTES + MAX_IIOD_LINE
MAX_PENDING_IN_URBS = 4096
MAX_CDC_BUFFER_BYTES = 256 * 1024
MAX_RNDIS_BUFFER_BYTES = 4 * 1024 * 1024
MASS_STORAGE_BLOCK_SIZE = 512
RNDIS_DEVICE_IP = "192.168.2.1"
RNDIS_HOST_IP = "192.168.2.10"
RNDIS_MTU = 1500
RNDIS_TCP_MSS = 1460
RNDIS_IIOD_PORT = 30431


def _padded_ascii(value: str, size: int) -> bytes:
    encoded = value.encode("ascii")
    if len(encoded) >= size:
        raise ValueError("USB/IP string does not fit its fixed field")
    return encoded + b"\0" * (size - len(encoded))


def _read_exact(stream, count: int) -> bytes:
    if count < 0 or count > MAX_URB_BYTES:
        raise USBProtocolError("USB/IP transfer length is out of range")
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError("USB/IP connection closed during a packet")
        data.extend(chunk)
    return bytes(data)


def _mac_bytes(address: str) -> bytes:
    fields = address.split(":")
    if len(fields) != 6:
        raise ValueError("invalid MAC address")
    return bytes(int(field, 16) for field in fields)


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    offset = cluster + cluster // 2
    value &= 0xFFF
    if cluster & 1:
        fat[offset] = (fat[offset] & 0x0F) | ((value << 4) & 0xF0)
        fat[offset + 1] = (value >> 4) & 0xFF
    else:
        fat[offset] = value & 0xFF
        fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)


def build_read_only_volume() -> bytes:
    """Return a deterministic 1.44 MiB FAT12 volume with one README file."""

    sectors = 2880
    image = bytearray(sectors * MASS_STORAGE_BLOCK_SIZE)
    boot = memoryview(image)[:MASS_STORAGE_BLOCK_SIZE]
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11, 512, 1, 1, 2, 224, sectors,
                     0xF0, 9, 18, 2, 0, 0)
    boot[36] = 0x00
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x4E465454)
    boot[43:54] = b"NEPTUNETWIN"
    boot[54:62] = b"FAT12   "
    boot[510:512] = b"\x55\xaa"

    fat = bytearray(9 * MASS_STORAGE_BLOCK_SIZE)
    fat[0:3] = b"\xf0\xff\xff"
    _set_fat12_entry(fat, 2, 0xFFF)
    first_fat = MASS_STORAGE_BLOCK_SIZE
    image[first_fat:first_fat + len(fat)] = fat
    second_fat = first_fat + len(fat)
    image[second_fat:second_fat + len(fat)] = fat

    content = (
        b"NeptuneSDR / HAMGEEK P210 virtual mass-storage contact\r\n"
        b"This volume is deterministic and read-only.\r\n"
        b"Use native IIO USB or Gigabit Ethernet for IQ and spectrum data.\r\n"
    )
    root_offset = (1 + 2 * 9) * MASS_STORAGE_BLOCK_SIZE
    entry = memoryview(image)[root_offset:root_offset + 32]
    entry[0:11] = b"README  TXT"
    entry[11] = 0x01
    struct.pack_into("<H", entry, 26, 2)
    struct.pack_into("<I", entry, 28, len(content))
    data_offset = (1 + 2 * 9 + 14) * MASS_STORAGE_BLOCK_SIZE
    image[data_offset:data_offset + len(content)] = content
    return bytes(image)


class _IIODBytePipe:
    """Incremental bulk-byte framing around one :class:`IIODSession`."""

    def __init__(self, context: IIOContext) -> None:
        self.session = IIODSession(context)
        self.input = bytearray()
        self.output = bytearray()
        self.pending_line: Optional[bytes] = None
        self.pending_payload = 0
        self.commands = 0

    def feed(self, data: bytes) -> None:
        if self.session.closed:
            raise USBProtocolError("native-IIO session is closed")
        if len(self.input) + len(data) > MAX_IIOD_BUFFERED_BYTES:
            raise USBProtocolError("native-IIO input exceeds its bounded buffer")
        self.input.extend(data)
        while True:
            if self.pending_line is not None:
                if len(self.input) < self.pending_payload:
                    return
                payload = bytes(self.input[:self.pending_payload])
                del self.input[:self.pending_payload]
                response = self.session.execute(self.pending_line, payload)
                self.pending_line = None
                self.pending_payload = 0
                self.commands += 1
                self._append_output(response)
                if self.session.closed:
                    self.input.clear()
                    return
                continue

            newline = self.input.find(b"\n")
            if newline < 0:
                if len(self.input) > MAX_IIOD_LINE:
                    self.input.clear()
                    self._append_output(b"-%d\n" % errno.E2BIG)
                return
            line = bytes(self.input[:newline + 1])
            del self.input[:newline + 1]
            if len(line) > MAX_IIOD_LINE:
                self._append_output(b"-%d\n" % errno.E2BIG)
                continue
            try:
                tokens = line.decode("ascii").strip().split()
            except UnicodeDecodeError:
                self._append_output(b"-%d\n" % errno.EINVAL)
                continue

            payload_length: Optional[int] = None
            is_writebuf = False
            if tokens and tokens[0].upper() == "WRITE" and tokens[-1].isdigit():
                payload_length = int(tokens[-1])
            elif tokens and tokens[0].upper() == "WRITEBUF" and len(tokens) == 3:
                try:
                    payload_length = int(tokens[2])
                except ValueError:
                    payload_length = None
                is_writebuf = True

            if payload_length is not None:
                if payload_length < 0 or payload_length > MAX_IIOD_PAYLOAD_BYTES:
                    self._append_output(self.session.execute(line, None))
                    continue
                if is_writebuf:
                    handshake = self.session.execute(line, None)
                    self._append_output(handshake)
                    if handshake.startswith(b"-"):
                        self.commands += 1
                        continue
                self.pending_line = line
                self.pending_payload = payload_length
                if not payload_length:
                    continue
                continue

            self.commands += 1
            self._append_output(self.session.execute(line))
            if self.session.closed:
                self.input.clear()
                return

    def _append_output(self, data: bytes) -> None:
        if len(self.output) + len(data) > MAX_IIOD_BUFFERED_BYTES:
            self.session.closed = True
            raise USBProtocolError("native-IIO output exceeds its bounded buffer")
        self.output.extend(data)

    def read(self, maximum: int) -> bytes:
        if maximum <= 0:
            return b""
        if not self.output:
            if self.session.closed:
                raise USBProtocolError("native-IIO session is closed")
            return b""
        count = min(maximum, len(self.output))
        result = bytes(self.output[:count])
        del self.output[:count]
        return result

    def close(self) -> None:
        self.input.clear()
        self.output.clear()
        self.session.closed = True


class _SocketIIODBytePipe:
    """Full-duplex native-IIO pipe bridged to a real TCP IIOD service."""

    def __init__(
        self,
        address: Tuple[str, int],
        data_ready: Callable[[], None],
        connect_timeout: float = 5.0,
    ) -> None:
        try:
            self.socket = socket.create_connection(address, timeout=connect_timeout)
        except OSError as exc:
            raise USBProtocolError("cannot connect native-IIO pipe to IIOD backend") from exc
        self.socket.settimeout(0.2)
        self.data_ready = data_ready
        self.output = bytearray()
        self.output_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.socket_closed = False
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._reader, name="p210-usb-iio-proxy", daemon=True
        )
        self.thread.start()

    def feed(self, data: bytes) -> None:
        if self.stop_event.is_set():
            raise USBProtocolError("IIOD backend is closed")
        try:
            with self.send_lock:
                self.socket.sendall(data)
        except OSError as exc:
            raise USBProtocolError("IIOD backend closed during USB bulk OUT") from exc

    def read(self, maximum: int) -> bytes:
        with self.output_lock:
            count = min(maximum, len(self.output))
            result = bytes(self.output[:count])
            del self.output[:count]
        if not result and self.stop_event.is_set():
            raise USBProtocolError("IIOD backend is closed")
        return result

    def close(self) -> None:
        self.stop_event.set()
        self._close_socket()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=1.0)

    def _close_socket(self) -> None:
        with self.close_lock:
            if self.socket_closed:
                return
            self.socket_closed = True
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()

    def _reader(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    data = self.socket.recv(64 * 1024)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                with self.output_lock:
                    if len(self.output) + len(data) > MAX_IIOD_BUFFERED_BYTES:
                        # A host that never submits IN URBs cannot grow memory
                        # without bound.  Closing is explicit and fail-closed.
                        self.stop_event.set()
                        break
                    self.output.extend(data)
                self.data_ready()
        finally:
            self.stop_event.set()
            self._close_socket()
            try:
                # Wake a queued IN URB so backend EOF becomes an EPIPE instead
                # of a request that remains pending forever.
                self.data_ready()
            except (OSError, USBProtocolError):
                pass


class _MassStorageBOT:
    """Small, strict, read-only SCSI transparent Bulk-Only target."""

    def __init__(self, image: Optional[bytes] = None) -> None:
        self.image = bytes(image if image is not None else build_read_only_volume())
        if len(self.image) % MASS_STORAGE_BLOCK_SIZE:
            raise ValueError("mass-storage image must contain whole sectors")
        self.output = bytearray()
        self.pending_discard = 0
        self.pending_csw: Optional[bytes] = None
        self.sense_key = 0
        self.sense_asc = 0
        self.commands = 0

    @property
    def blocks(self) -> int:
        return len(self.image) // MASS_STORAGE_BLOCK_SIZE

    def reset(self) -> None:
        self.output.clear()
        self.pending_discard = 0
        self.pending_csw = None
        self.sense_key = 0
        self.sense_asc = 0

    def feed(self, data: bytes) -> None:
        remaining = memoryview(bytes(data))
        while remaining:
            if self.pending_discard:
                count = min(self.pending_discard, len(remaining))
                remaining = remaining[count:]
                self.pending_discard -= count
                if not self.pending_discard and self.pending_csw is not None:
                    self._append_output(self.pending_csw)
                    self.pending_csw = None
                continue
            if len(remaining) < _CBW.size:
                self.sense_key, self.sense_asc = 0x05, 0x2400
                raise USBProtocolError("mass-storage CBW is truncated")
            cbw = bytes(remaining[:_CBW.size])
            remaining = remaining[_CBW.size:]
            signature, tag, transfer_length, flags, lun, cdb_length, cdb = _CBW.unpack(cbw)
            if (
                signature != b"USBC"
                or flags & 0x7F
                or lun != 0
                or not 1 <= cdb_length <= 16
            ):
                raise USBProtocolError("invalid mass-storage CBW")
            self.commands += 1
            direction_in = bool(flags & 0x80)
            command = cdb[0]
            data_in_commands = {0x03, 0x12, 0x1A, 0x23, 0x25, 0x28, 0x5A, 0x9E}
            no_data_commands = {0x00, 0x1B, 0x1E, 0x35}
            phase_error = bool(
                transfer_length
                and (
                    (command in data_in_commands and not direction_in)
                    or (command in no_data_commands)
                    or (command in (0x2A, 0xAA) and direction_in)
                )
            )
            data_in, status = self._execute(
                cdb[:cdb_length], transfer_length, direction_in
            )
            if phase_error:
                data_in = b""
                status = 2
            actual = min(len(data_in), transfer_length)
            residue = transfer_length - actual
            csw = _CSW.pack(b"USBS", tag, residue, status)
            if data_in:
                self._append_output(data_in[:transfer_length])
            if not direction_in and transfer_length:
                self.pending_discard = transfer_length
                self.pending_csw = csw
            else:
                self._append_output(csw)

    def _append_output(self, data: bytes) -> None:
        if len(self.output) + len(data) > MAX_URB_BYTES + _CSW.size:
            raise USBProtocolError("mass-storage response queue is full")
        self.output.extend(data)

    def read(self, maximum: int) -> bytes:
        if maximum <= 0 or not self.output:
            return b""
        count = min(maximum, len(self.output))
        data = bytes(self.output[:count])
        del self.output[:count]
        return data

    def _execute(self, cdb: bytes, transfer_length: int, direction_in: bool) -> Tuple[bytes, int]:
        command = cdb[0]
        if command == 0x00:  # TEST UNIT READY
            return b"", 0
        if command == 0x12:  # INQUIRY
            allocation = cdb[4] if len(cdb) >= 5 else transfer_length
            response = bytearray(36)
            response[0] = 0x00
            response[1] = 0x80
            response[2] = 0x05
            response[3] = 0x02
            response[4] = 31
            response[8:16] = b"NEPTUNE "
            response[16:32] = b"P210 TWIN DISK  "
            response[32:36] = b"1.00"
            return bytes(response[:allocation]), 0
        if command == 0x03:  # REQUEST SENSE
            allocation = cdb[4] if len(cdb) >= 5 else transfer_length
            response = bytearray(18)
            response[0] = 0x70
            response[2] = self.sense_key
            response[7] = 10
            response[12] = (self.sense_asc >> 8) & 0xFF
            response[13] = self.sense_asc & 0xFF
            self.sense_key = self.sense_asc = 0
            return bytes(response[:allocation]), 0
        if command == 0x25:  # READ CAPACITY (10)
            return struct.pack("!II", self.blocks - 1, MASS_STORAGE_BLOCK_SIZE), 0
        if command == 0x23:  # READ FORMAT CAPACITIES
            response = b"\0\0\0\x08" + struct.pack("!I", self.blocks) + b"\x02\0\x02\0"
            return response[:transfer_length], 0
        if command == 0x28 and len(cdb) >= 10:  # READ (10)
            lba = struct.unpack_from("!I", cdb, 2)[0]
            blocks = struct.unpack_from("!H", cdb, 7)[0]
            if lba > self.blocks or blocks > self.blocks - lba:
                self.sense_key, self.sense_asc = 0x05, 0x2100
                return b"", 1
            start = lba * MASS_STORAGE_BLOCK_SIZE
            end = start + blocks * MASS_STORAGE_BLOCK_SIZE
            return self.image[start:end], 0
        if command == 0x1A:  # MODE SENSE (6), write-protected
            return b"\x03\0\x80\0"[:transfer_length], 0
        if command == 0x5A:  # MODE SENSE (10), write-protected
            return b"\0\x06\0\x80\0\0\0\0"[:transfer_length], 0
        if command in (0x1B, 0x1E, 0x35):
            return b"", 0
        if command == 0x9E and len(cdb) >= 16 and (cdb[1] & 0x1F) == 0x10:
            response = bytearray(32)
            struct.pack_into("!Q", response, 0, self.blocks - 1)
            struct.pack_into("!I", response, 8, MASS_STORAGE_BLOCK_SIZE)
            return bytes(response[:transfer_length]), 0
        if command in (0x2A, 0xAA):
            self.sense_key, self.sense_asc = 0x07, 0x2700
            return b"", 1
        self.sense_key, self.sense_asc = 0x05, 0x2000
        return b"", 1


class _CDCConsole:
    def __init__(self) -> None:
        self.line_coding = bytearray(struct.pack("<IBBB", 115200, 0, 0, 8))
        self.control_line_state = 0
        self.input = bytearray()
        self.output = bytearray(b"NeptuneSDR P210 virtual console\r\nroot@neptune:~# ")
        self.commands = 0
        self.suppress_lf = False
        self.notifications: Deque[bytes] = deque()
        self.reset_notifications()

    def reset_notifications(self) -> None:
        self.notifications.clear()
        self.notifications.append(b"\xa1\x20\0\0\x03\0\x02\0\x03\0")

    def queue_serial_state(self) -> None:
        if not self.notifications:
            self.notifications.append(b"\xa1\x20\0\0\x03\0\x02\0\x03\0")

    def read_notification(self, maximum: int) -> bytes:
        if not self.notifications:
            return b""
        if maximum < len(self.notifications[0]):
            raise USBProtocolError("CDC notification URB is too small")
        return self.notifications.popleft()

    def feed(self, data: bytes) -> None:
        if self.suppress_lf:
            self.suppress_lf = False
            if data.startswith(b"\n"):
                data = data[1:]
        self.input.extend(data)
        while True:
            newline_positions = [p for p in (self.input.find(b"\n"), self.input.find(b"\r")) if p >= 0]
            if not newline_positions:
                if len(self.input) > MAX_CDC_BUFFER_BYTES:
                    self.input.clear()
                    raise USBProtocolError("CDC console line exceeds its bounded buffer")
                return
            end = min(newline_positions)
            terminator = self.input[end]
            line = bytes(self.input[:end]).decode("utf-8", errors="replace").strip()
            consume = end + 1
            while consume < len(self.input) and self.input[consume] in (10, 13):
                consume += 1
            self.suppress_lf = terminator == 13 and consume == len(self.input)
            del self.input[:consume]
            self.commands += 1
            if line == "help":
                answer = "help uname iio-version fft-status"
            elif line in ("uname", "uname -a"):
                answer = "Linux neptune 4.14.0-g387d584 #1 SMP ARMv7l GNU/Linux"
            elif line == "iio-version":
                answer = "0.26.v0.26"
            elif line == "fft-status":
                answer = "N=65536 channels=2 sample_rate=61440000 bandwidth=50000000"
            elif line:
                answer = "sh: %s: not found" % line.split()[0]
            else:
                answer = ""
            response = (answer + "\r\nroot@neptune:~# ").encode("utf-8")
            if len(self.output) + len(response) > MAX_CDC_BUFFER_BYTES:
                raise USBProtocolError("CDC console response queue is full")
            self.output.extend(response)

    def read(self, maximum: int) -> bytes:
        count = min(maximum, len(self.output))
        result = bytes(self.output[:count])
        del self.output[:count]
        return result


class _RNDIS:
    def __init__(self, host_mac: bytes, device_mac: bytes) -> None:
        if len(host_mac) != 6 or len(device_mac) != 6:
            raise ValueError("RNDIS MAC addresses must be six bytes")
        # The OID is consumed by the USB host and therefore exposes Linux
        # gadget's host_addr.  Ethernet replies originate from dev_addr.
        self.mac = host_mac
        self.device_mac = device_mac
        self.control_output = bytearray()
        self.control_notifications = 0
        self.data_output: Deque[bytes] = deque()
        self.data_output_bytes = 0
        self.data_lock = threading.RLock()
        self.received_frames: Deque[bytes] = deque(maxlen=256)
        self.packet_filter = 0
        self.tx_packets = 0
        self.rx_packets = 0
        self.rx_dropped = 0
        self._oids = (
            OID_GEN_SUPPORTED_LIST, OID_GEN_HARDWARE_STATUS, OID_GEN_MEDIA_SUPPORTED,
            OID_GEN_MEDIA_IN_USE, OID_GEN_MAXIMUM_LOOKAHEAD, OID_GEN_MAXIMUM_FRAME_SIZE,
            OID_GEN_LINK_SPEED, OID_GEN_TRANSMIT_BLOCK_SIZE, OID_GEN_RECEIVE_BLOCK_SIZE,
            OID_GEN_VENDOR_ID, OID_GEN_VENDOR_DESCRIPTION, OID_GEN_CURRENT_PACKET_FILTER,
            OID_GEN_CURRENT_LOOKAHEAD, OID_GEN_DRIVER_VERSION, OID_GEN_MAXIMUM_TOTAL_SIZE,
            OID_GEN_MEDIA_CONNECT_STATUS, OID_GEN_PHYSICAL_MEDIUM, OID_GEN_XMIT_OK,
            OID_GEN_RCV_OK, OID_GEN_XMIT_ERROR, OID_GEN_RCV_ERROR, OID_GEN_RCV_NO_BUFFER,
            OID_802_3_PERMANENT_ADDRESS, OID_802_3_CURRENT_ADDRESS,
            OID_802_3_MULTICAST_LIST, OID_802_3_MAXIMUM_LIST_SIZE,
            OID_802_3_MAC_OPTIONS, OID_802_3_RCV_ERROR_ALIGNMENT,
            OID_802_3_XMIT_ONE_COLLISION, OID_802_3_XMIT_MORE_COLLISIONS,
        )

    def reset(self) -> None:
        self.control_output.clear()
        self.control_notifications = 0
        with self.data_lock:
            self.data_output.clear()
            self.data_output_bytes = 0
            self.received_frames.clear()
        self.packet_filter = 0

    def control_command(self, message: bytes) -> None:
        output_before = len(self.control_output)
        if len(message) < 8:
            raise USBProtocolError("RNDIS control message is truncated")
        kind, length = struct.unpack_from("<II", message)
        if length < 8 or length > len(message):
            raise USBProtocolError("RNDIS control message length is invalid")
        message = message[:length]
        minimum_lengths = {
            RNDIS_INITIALIZE_MSG: 24,
            RNDIS_HALT_MSG: 12,
            RNDIS_QUERY_MSG: 28,
            RNDIS_SET_MSG: 28,
            RNDIS_RESET_MSG: 8,
            RNDIS_KEEPALIVE_MSG: 12,
        }
        if kind not in minimum_lengths or length < minimum_lengths[kind]:
            raise USBProtocolError("RNDIS control message is truncated")
        request_id = struct.unpack_from("<I", message, 8)[0] if len(message) >= 12 else 0
        if kind == RNDIS_INITIALIZE_MSG:
            self.control_output.extend(struct.pack(
                "<13I", RNDIS_INITIALIZE_CMPLT, 52, request_id, RNDIS_STATUS_SUCCESS,
                1, 0, 1, 0, 1, 0x4000, 0, 0, 0,
            ))
        elif kind == RNDIS_QUERY_MSG:
            oid = struct.unpack_from("<I", message, 12)[0]
            info_length, info_offset = struct.unpack_from("<II", message, 16)
            info_start = 8 + info_offset
            if info_length and (
                info_start < 28 or info_start + info_length > len(message)
            ):
                raise USBProtocolError("RNDIS query information buffer is invalid")
            value = self._query(oid)
            status = RNDIS_STATUS_SUCCESS if value is not None else RNDIS_STATUS_NOT_SUPPORTED
            data = value or b""
            self.control_output.extend(struct.pack(
                "<6I", RNDIS_QUERY_CMPLT, 24 + len(data), request_id, status,
                len(data), 16 if data else 0,
            ) + data)
        elif kind == RNDIS_SET_MSG:
            oid, info_length, info_offset = struct.unpack_from("<III", message, 12)
            start = 8 + info_offset
            end = start + info_length
            status = RNDIS_STATUS_NOT_SUPPORTED
            if start < 28 or end > len(message):
                raise USBProtocolError("RNDIS set information buffer is invalid")
            if oid in (OID_GEN_CURRENT_PACKET_FILTER, OID_802_3_MULTICAST_LIST):
                if oid == OID_GEN_CURRENT_PACKET_FILTER and info_length >= 4:
                    self.packet_filter = struct.unpack_from("<I", message, start)[0]
                status = RNDIS_STATUS_SUCCESS
            self.control_output.extend(struct.pack(
                "<4I", RNDIS_SET_CMPLT, 16, request_id, status,
            ))
        elif kind == RNDIS_RESET_MSG:
            self.packet_filter = 0
            self.control_output.extend(struct.pack(
                "<4I", RNDIS_RESET_CMPLT, 16, RNDIS_STATUS_SUCCESS, 1,
            ))
        elif kind == RNDIS_KEEPALIVE_MSG:
            self.control_output.extend(struct.pack(
                "<4I", RNDIS_KEEPALIVE_CMPLT, 16, request_id, RNDIS_STATUS_SUCCESS,
            ))
        elif kind == RNDIS_HALT_MSG:
            self.packet_filter = 0
        else:  # pragma: no cover - guarded by minimum_lengths above
            raise USBProtocolError("unsupported RNDIS control message")
        if len(self.control_output) > output_before:
            self.control_notifications += 1

    def control_response(self, maximum: int) -> bytes:
        count = min(maximum, len(self.control_output))
        data = bytes(self.control_output[:count])
        del self.control_output[:count]
        return data

    def take_control_notification(self, maximum: int) -> bytes:
        if not self.control_notifications:
            return b""
        notification = b"\xa1\x01\0\0\0\0\0\0"
        if maximum < len(notification):
            raise USBProtocolError("RNDIS notification URB is too small")
        self.control_notifications -= 1
        return notification

    def feed_packets(self, data: bytes) -> Tuple[bytes, ...]:
        offset = 0
        accepted = []
        while offset < len(data):
            if len(data) - offset < 44:
                raise USBProtocolError("RNDIS packet message is truncated")
            fields = struct.unpack_from("<11I", data, offset)
            kind, message_length, data_offset, data_length = fields[:4]
            if kind != RNDIS_PACKET_MSG or message_length < 44 or offset + message_length > len(data):
                raise USBProtocolError("invalid RNDIS packet message")
            start = offset + 8 + data_offset
            end = start + data_length
            if (
                start < offset + 44
                or end > offset + message_length
                or not 14 <= data_length <= 1514
            ):
                raise USBProtocolError("RNDIS Ethernet frame exceeds its message")
            if len(self.received_frames) == self.received_frames.maxlen:
                self.rx_dropped += 1
            else:
                frame = bytes(data[start:end])
                self.received_frames.append(frame)
                accepted.append(frame)
                self.rx_packets += 1
            offset += message_length
        return tuple(accepted)

    def queue_frame(self, frame: bytes) -> None:
        payload = bytes(frame)
        if not 14 <= len(payload) <= 1514:
            raise USBProtocolError("RNDIS Ethernet frame length is invalid")
        header = struct.pack("<11I", RNDIS_PACKET_MSG, 44 + len(payload), 36, len(payload),
                             0, 0, 0, 0, 0, 0, 0)
        message = header + payload
        with self.data_lock:
            if self.data_output_bytes + len(message) > MAX_RNDIS_BUFFER_BYTES:
                self.rx_dropped += 1
                raise USBProtocolError("RNDIS transmit queue is full")
            self.data_output.append(message)
            self.data_output_bytes += len(message)
        self.tx_packets += 1

    def read_packets(self, maximum: int) -> bytes:
        with self.data_lock:
            if not self.data_output or maximum <= 0:
                return b""
            if len(self.data_output[0]) > maximum:
                raise USBProtocolError("RNDIS IN URB cannot hold one complete frame")
            messages = []
            length = 0
            while self.data_output and length + len(self.data_output[0]) <= maximum:
                message = self.data_output.popleft()
                messages.append(message)
                length += len(message)
                self.data_output_bytes -= len(message)
            return b"".join(messages)

    def available_output_bytes(self) -> int:
        with self.data_lock:
            return MAX_RNDIS_BUFFER_BYTES - self.data_output_bytes

    def _query(self, oid: int) -> Optional[bytes]:
        u32 = lambda value: struct.pack("<I", value)
        values = {
            OID_GEN_SUPPORTED_LIST: b"".join(u32(item) for item in self._oids),
            OID_GEN_HARDWARE_STATUS: u32(0),
            OID_GEN_MEDIA_SUPPORTED: u32(0),
            OID_GEN_MEDIA_IN_USE: u32(0),
            OID_GEN_MAXIMUM_LOOKAHEAD: u32(1500),
            OID_GEN_MAXIMUM_FRAME_SIZE: u32(1500),
            OID_GEN_LINK_SPEED: u32(1_000_000),
            OID_GEN_TRANSMIT_BLOCK_SIZE: u32(1514),
            OID_GEN_RECEIVE_BLOCK_SIZE: u32(1514),
            OID_GEN_VENDOR_ID: u32(int.from_bytes(self.mac[:3], "big")),
            OID_GEN_VENDOR_DESCRIPTION: b"NeptuneSDR P210 Twin\0",
            OID_GEN_CURRENT_PACKET_FILTER: u32(self.packet_filter),
            OID_GEN_CURRENT_LOOKAHEAD: u32(1500),
            OID_GEN_DRIVER_VERSION: u32(0x00010000),
            OID_GEN_MAXIMUM_TOTAL_SIZE: u32(1558),
            OID_GEN_MEDIA_CONNECT_STATUS: u32(0),
            OID_GEN_PHYSICAL_MEDIUM: u32(0),
            OID_GEN_XMIT_OK: u32(self.tx_packets),
            OID_GEN_RCV_OK: u32(self.rx_packets),
            OID_GEN_XMIT_ERROR: u32(0),
            OID_GEN_RCV_ERROR: u32(0),
            OID_GEN_RCV_NO_BUFFER: u32(self.rx_dropped),
            OID_802_3_PERMANENT_ADDRESS: self.mac,
            OID_802_3_CURRENT_ADDRESS: self.mac,
            OID_802_3_MULTICAST_LIST: b"",
            OID_802_3_MAXIMUM_LIST_SIZE: u32(1),
            OID_802_3_MAC_OPTIONS: u32(0),
            OID_802_3_RCV_ERROR_ALIGNMENT: u32(0),
            OID_802_3_XMIT_ONE_COLLISION: u32(0),
            OID_802_3_XMIT_MORE_COLLISIONS: u32(0),
        }
        return values.get(oid)


def _internet_checksum(data: bytes) -> int:
    payload = bytes(data)
    if len(payload) & 1:
        payload += b"\0"
    total = sum(struct.unpack("!%dH" % (len(payload) // 2), payload))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _transport_checksum(source: bytes, destination: bytes, protocol: int, data: bytes) -> int:
    pseudo = source + destination + struct.pack("!BBH", 0, protocol, len(data))
    checksum = _internet_checksum(pseudo + data)
    return checksum or 0xFFFF


def _valid_transport_checksum(
    source: bytes, destination: bytes, protocol: int, data: bytes
) -> bool:
    pseudo = source + destination + struct.pack("!BBH", 0, protocol, len(data))
    return _internet_checksum(pseudo + data) == 0


@dataclass
class _RNDISTCPConnection:
    client_mac: bytes
    client_ip: bytes
    client_port: int
    client_next: int
    server_isn: int
    server_next: int
    server_acked: int
    peer_window: int
    peer_mss: int
    pipe: object
    state: str = "syn-received"
    backend_closed: bool = False
    fin_sent: bool = False


class _RNDISNetwork:
    """Bounded IPv4 service behind the composite gadget's RNDIS function.

    The TCP implementation intentionally accepts one in-order IIOD session.
    USB is reliable and ordered, so packet loss recovery is not emulated; an
    out-of-order segment receives a duplicate ACK and is never passed twice to
    IIOD.  Other TCP ports are rejected with RST.
    """

    TCP_FIN = 0x01
    TCP_SYN = 0x02
    TCP_RST = 0x04
    TCP_PSH = 0x08
    TCP_ACK = 0x10

    def __init__(
        self,
        rndis: _RNDIS,
        pipe_factory: Callable[[Callable[[], None]], object],
        data_ready: Callable[[], None],
        device_ip: str,
        host_ip: str,
    ) -> None:
        self.rndis = rndis
        self.pipe_factory = pipe_factory
        self.data_ready = data_ready
        self.device_mac = rndis.device_mac
        self.device_ip_text = str(ipaddress.IPv4Address(device_ip))
        self.host_ip_text = str(ipaddress.IPv4Address(host_ip))
        self.device_ip = ipaddress.IPv4Address(self.device_ip_text).packed
        self.host_ip = ipaddress.IPv4Address(self.host_ip_text).packed
        if self.device_ip == self.host_ip or self.device_ip[:3] != self.host_ip[:3]:
            raise ValueError("RNDIS device and host addresses must be distinct in one /24")
        self.lock = threading.RLock()
        self.tcp: Optional[_RNDISTCPConnection] = None
        self.closing = False
        self.ip_identifier = 1
        self.connection_number = 0
        self.stats = {
            "rx_frames": 0,
            "dropped_frames": 0,
            "arp_replies": 0,
            "icmp_replies": 0,
            "dhcp_replies": 0,
            "tcp_segments": 0,
            "tcp_resets": 0,
        }

    def snapshot(self) -> Mapping[str, object]:
        with self.lock:
            connection = self.tcp
            return {
                "device_mac": ":".join("%02x" % byte for byte in self.device_mac),
                "device_ip": self.device_ip_text,
                "host_ip": self.host_ip_text,
                "subnet_mask": "255.255.255.0",
                "iiod_port": RNDIS_IIOD_PORT,
                "tcp_state": connection.state if connection is not None else "closed",
                "stats": dict(self.stats),
            }

    def reset(self) -> None:
        with self.lock:
            connection = self.tcp
            self.tcp = None
            self.closing = True
        if connection is not None:
            connection.pipe.close()  # type: ignore[attr-defined]
        with self.lock:
            self.closing = False

    close = reset

    def handle_frame(self, frame: bytes) -> None:
        close_pipe = None
        with self.lock:
            self.stats["rx_frames"] += 1
            if len(frame) < 14:
                self.stats["dropped_frames"] += 1
                return
            destination, source, ether_type = frame[:6], frame[6:12], struct.unpack(
                "!H", frame[12:14]
            )[0]
            if destination not in (self.device_mac, b"\xff" * 6):
                self.stats["dropped_frames"] += 1
                return
            if ether_type == 0x0806:
                self._handle_arp(source, frame[14:])
            elif ether_type == 0x0800:
                close_pipe = self._handle_ipv4(source, frame[14:])
            else:
                # IPv6 and all non-IPv4 EtherTypes are deliberately absent
                # from this narrow Pluto-compatible management network.
                self.stats["dropped_frames"] += 1
        if close_pipe is not None:
            close_pipe.close()  # type: ignore[attr-defined]

    def on_usb_drain(self) -> None:
        with self.lock:
            if self.tcp is not None:
                self._drain_backend_locked(self.tcp)

    def _backend_ready(self) -> None:
        with self.lock:
            if self.closing or self.tcp is None:
                return
            self._drain_backend_locked(self.tcp)
        try:
            self.data_ready()
        except (OSError, USBProtocolError):
            pass

    def _queue_ethernet(self, destination: bytes, ether_type: int, payload: bytes) -> None:
        self.rndis.queue_frame(
            destination + self.device_mac + struct.pack("!H", ether_type) + payload
        )

    def _build_ipv4(self, destination: bytes, protocol: int, payload: bytes) -> bytes:
        total_length = 20 + len(payload)
        identifier = self.ip_identifier & 0xFFFF
        self.ip_identifier = (self.ip_identifier + 1) & 0xFFFF
        header = bytearray(
            struct.pack(
                "!BBHHHBBH4s4s",
                0x45,
                0,
                total_length,
                identifier,
                0x4000,
                64,
                protocol,
                0,
                self.device_ip,
                destination,
            )
        )
        struct.pack_into("!H", header, 10, _internet_checksum(header))
        return bytes(header) + payload

    def _handle_arp(self, source_mac: bytes, packet: bytes) -> None:
        if len(packet) < 28:
            self.stats["dropped_frames"] += 1
            return
        hardware, protocol, hardware_len, protocol_len, operation = struct.unpack(
            "!HHBBH", packet[:8]
        )
        sender_mac = packet[8:14]
        sender_ip = packet[14:18]
        target_ip = packet[24:28]
        if (
            hardware != 1
            or protocol != 0x0800
            or hardware_len != 6
            or protocol_len != 4
            or operation != 1
            or sender_mac != source_mac
            or target_ip != self.device_ip
        ):
            self.stats["dropped_frames"] += 1
            return
        reply = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2)
        reply += self.device_mac + self.device_ip + sender_mac + sender_ip
        self._queue_ethernet(sender_mac, 0x0806, reply)
        self.stats["arp_replies"] += 1

    def _parse_ipv4(self, packet: bytes):
        if len(packet) < 20 or packet[0] >> 4 != 4:
            return None
        header_length = (packet[0] & 0x0F) * 4
        if header_length < 20 or header_length > len(packet):
            return None
        total_length = struct.unpack_from("!H", packet, 2)[0]
        fragment = struct.unpack_from("!H", packet, 6)[0]
        if (
            total_length < header_length
            or total_length > len(packet)
            or fragment & 0x3FFF
            or _internet_checksum(packet[:header_length]) != 0
        ):
            return None
        return (
            packet[12:16],
            packet[16:20],
            packet[9],
            packet[header_length:total_length],
        )

    def _handle_ipv4(self, source_mac: bytes, packet: bytes):
        parsed = self._parse_ipv4(packet)
        if parsed is None:
            self.stats["dropped_frames"] += 1
            return None
        source_ip, destination_ip, protocol, payload = parsed
        if destination_ip not in (self.device_ip, b"\xff\xff\xff\xff"):
            self.stats["dropped_frames"] += 1
            return None
        if protocol == 1:
            self._handle_icmp(source_mac, source_ip, destination_ip, payload)
            return None
        if protocol == 17:
            self._handle_udp(source_mac, source_ip, destination_ip, payload)
            return None
        if protocol == 6 and destination_ip == self.device_ip:
            return self._handle_tcp(source_mac, source_ip, payload)
        self.stats["dropped_frames"] += 1
        return None

    def _handle_icmp(
        self, source_mac: bytes, source_ip: bytes, destination_ip: bytes, packet: bytes
    ) -> None:
        if (
            destination_ip != self.device_ip
            or len(packet) < 8
            or packet[0] != 8
            or packet[1] != 0
            or _internet_checksum(packet) != 0
        ):
            self.stats["dropped_frames"] += 1
            return
        reply = bytearray(packet)
        reply[0] = 0
        reply[2:4] = b"\0\0"
        struct.pack_into("!H", reply, 2, _internet_checksum(reply))
        self._queue_ethernet(
            source_mac, 0x0800, self._build_ipv4(source_ip, 1, bytes(reply))
        )
        self.stats["icmp_replies"] += 1

    @staticmethod
    def _dhcp_options(data: bytes) -> Optional[Dict[int, bytes]]:
        options: Dict[int, bytes] = {}
        position = 0
        while position < len(data):
            code = data[position]
            position += 1
            if code == 0:
                continue
            if code == 255:
                return options
            if position >= len(data):
                return None
            length = data[position]
            position += 1
            if position + length > len(data):
                return None
            options[code] = data[position:position + length]
            position += length
        return None

    def _handle_udp(
        self, source_mac: bytes, source_ip: bytes, destination_ip: bytes, packet: bytes
    ) -> None:
        if len(packet) < 8:
            self.stats["dropped_frames"] += 1
            return
        source_port, destination_port, length, checksum = struct.unpack("!HHHH", packet[:8])
        if length < 8 or length > len(packet):
            self.stats["dropped_frames"] += 1
            return
        datagram = packet[:length]
        if checksum and not _valid_transport_checksum(
            source_ip, destination_ip, 17, datagram
        ):
            self.stats["dropped_frames"] += 1
            return
        if (source_port, destination_port) != (68, 67):
            self.stats["dropped_frames"] += 1
            return
        self._handle_dhcp(source_mac, source_ip, datagram[8:])

    def _handle_dhcp(self, source_mac: bytes, source_ip: bytes, message: bytes) -> None:
        if (
            len(message) < 240
            or message[0] != 1
            or message[1] != 1
            or message[2] != 6
            or message[28:34] != source_mac
            or message[236:240] != b"\x63\x82\x53\x63"
        ):
            self.stats["dropped_frames"] += 1
            return
        options = self._dhcp_options(message[240:])
        if options is None or len(options.get(53, b"")) != 1:
            self.stats["dropped_frames"] += 1
            return
        message_type = options[53][0]
        server_id = options.get(54)
        if server_id is not None and server_id != self.device_ip:
            return
        if message_type == 1:
            reply_type = 2  # DHCPOFFER
        elif message_type == 3:
            requested = options.get(50, message[12:16])
            reply_type = 5 if requested in (self.host_ip, b"\0\0\0\0") else 6
        elif message_type == 8:
            reply_type = 5  # DHCPACK for INFORM
        else:
            return

        reply = bytearray(240)
        reply[0:4] = b"\x02\x01\x06\x00"
        reply[4:8] = message[4:8]
        reply[10:12] = message[10:12]
        if reply_type in (2, 5) and message_type != 8:
            reply[16:20] = self.host_ip
        reply[20:24] = self.device_ip
        reply[28:44] = message[28:44]
        reply[236:240] = b"\x63\x82\x53\x63"
        option_bytes = b"\x35\x01" + bytes((reply_type,))
        option_bytes += b"\x36\x04" + self.device_ip
        if reply_type != 6:
            option_bytes += b"\x01\x04\xff\xff\xff\x00"
            option_bytes += b"\x33\x04" + struct.pack("!I", 86400)
            option_bytes += b"\x3a\x04" + struct.pack("!I", 43200)
            option_bytes += b"\x3b\x04" + struct.pack("!I", 75600)
        option_bytes += b"\xff"
        bootp = bytes(reply) + option_bytes
        udp_length = 8 + len(bootp)
        udp = bytearray(struct.pack("!HHHH", 67, 68, udp_length, 0) + bootp)
        destination_ip = b"\xff\xff\xff\xff"
        udp_checksum = _transport_checksum(self.device_ip, destination_ip, 17, udp)
        struct.pack_into("!H", udp, 6, udp_checksum)
        self._queue_ethernet(
            b"\xff" * 6,
            0x0800,
            self._build_ipv4(destination_ip, 17, bytes(udp)),
        )
        self.stats["dhcp_replies"] += 1

    @staticmethod
    def _tcp_mss(options: bytes) -> int:
        position = 0
        while position < len(options):
            kind = options[position]
            if kind == 0:
                break
            if kind == 1:
                position += 1
                continue
            if position + 1 >= len(options):
                break
            length = options[position + 1]
            if length < 2 or position + length > len(options):
                break
            if kind == 2 and length == 4:
                return max(256, min(RNDIS_TCP_MSS, struct.unpack("!H", options[position + 2:position + 4])[0]))
            position += length
        return 536

    def _build_tcp(
        self,
        destination_ip: bytes,
        source_port: int,
        destination_port: int,
        sequence: int,
        acknowledgement: int,
        flags: int,
        window: int,
        payload: bytes = b"",
        options: bytes = b"",
    ) -> bytes:
        if len(options) & 3:
            options += b"\0" * (-len(options) & 3)
        offset = (20 + len(options)) // 4
        header = bytearray(
            struct.pack(
                "!HHIIBBHHH",
                source_port,
                destination_port,
                sequence & 0xFFFFFFFF,
                acknowledgement & 0xFFFFFFFF,
                offset << 4,
                flags,
                max(0, min(0xFFFF, window)),
                0,
                0,
            )
            + options
        )
        segment = header + payload
        struct.pack_into(
            "!H",
            segment,
            16,
            _transport_checksum(self.device_ip, destination_ip, 6, segment),
        )
        return bytes(segment)

    def _send_tcp_raw(
        self,
        destination_mac: bytes,
        destination_ip: bytes,
        source_port: int,
        destination_port: int,
        sequence: int,
        acknowledgement: int,
        flags: int,
        window: int = 65535,
        payload: bytes = b"",
        options: bytes = b"",
    ) -> None:
        tcp = self._build_tcp(
            destination_ip,
            source_port,
            destination_port,
            sequence,
            acknowledgement,
            flags,
            window,
            payload,
            options,
        )
        self._queue_ethernet(
            destination_mac, 0x0800, self._build_ipv4(destination_ip, 6, tcp)
        )
        self.stats["tcp_segments"] += 1

    def _send_tcp(
        self,
        connection: _RNDISTCPConnection,
        sequence: int,
        acknowledgement: int,
        flags: int,
        payload: bytes = b"",
        options: bytes = b"",
    ) -> None:
        self._send_tcp_raw(
            connection.client_mac,
            connection.client_ip,
            RNDIS_IIOD_PORT,
            connection.client_port,
            sequence,
            acknowledgement,
            flags,
            payload=payload,
            options=options,
        )

    def _send_tcp_reset(
        self,
        source_mac: bytes,
        source_ip: bytes,
        source_port: int,
        destination_port: int,
        sequence: int,
        acknowledgement: int,
        flags: int,
        payload_length: int,
    ) -> None:
        if flags & self.TCP_RST:
            return
        if flags & self.TCP_ACK:
            reset_sequence = acknowledgement
            reset_ack = 0
            reset_flags = self.TCP_RST
        else:
            reset_sequence = 0
            reset_ack = sequence + payload_length
            if flags & self.TCP_SYN:
                reset_ack += 1
            if flags & self.TCP_FIN:
                reset_ack += 1
            reset_flags = self.TCP_RST | self.TCP_ACK
        self._send_tcp_raw(
            source_mac,
            source_ip,
            destination_port,
            source_port,
            reset_sequence,
            reset_ack,
            reset_flags,
            window=0,
        )
        self.stats["tcp_resets"] += 1

    def _detach_tcp_locked(self, connection: _RNDISTCPConnection):
        if self.tcp is connection:
            self.tcp = None
            return connection.pipe
        return None

    def _handle_tcp(self, source_mac: bytes, source_ip: bytes, segment: bytes):
        if len(segment) < 20:
            self.stats["dropped_frames"] += 1
            return None
        (
            source_port,
            destination_port,
            sequence,
            acknowledgement,
            offset_byte,
            flags,
            window,
            checksum,
            urgent,
        ) = struct.unpack("!HHIIBBHHH", segment[:20])
        header_length = (offset_byte >> 4) * 4
        if (
            header_length < 20
            or header_length > len(segment)
            or not _valid_transport_checksum(source_ip, self.device_ip, 6, segment)
        ):
            self.stats["dropped_frames"] += 1
            return None
        payload = segment[header_length:]
        if destination_port != RNDIS_IIOD_PORT:
            self._send_tcp_reset(
                source_mac,
                source_ip,
                source_port,
                destination_port,
                sequence,
                acknowledgement,
                flags,
                len(payload),
            )
            return None

        connection = self.tcp
        matches = bool(
            connection is not None
            and connection.client_mac == source_mac
            and connection.client_ip == source_ip
            and connection.client_port == source_port
        )
        if flags & self.TCP_SYN and not flags & self.TCP_ACK:
            if matches and connection is not None and connection.state == "syn-received":
                self._send_tcp(
                    connection,
                    connection.server_isn,
                    connection.client_next,
                    self.TCP_SYN | self.TCP_ACK,
                    options=b"\x02\x04" + struct.pack("!H", RNDIS_TCP_MSS),
                )
                return None
            if connection is not None:
                self._send_tcp_reset(
                    source_mac,
                    source_ip,
                    source_port,
                    destination_port,
                    sequence,
                    acknowledgement,
                    flags,
                    len(payload),
                )
                return None
            try:
                pipe = self.pipe_factory(self._backend_ready)
            except USBProtocolError:
                self._send_tcp_reset(
                    source_mac,
                    source_ip,
                    source_port,
                    destination_port,
                    sequence,
                    acknowledgement,
                    flags,
                    len(payload),
                )
                return None
            self.connection_number += 1
            server_isn = (0x4E505400 + self.connection_number * 0x10000) & 0xFFFFFFFF
            connection = _RNDISTCPConnection(
                client_mac=source_mac,
                client_ip=source_ip,
                client_port=source_port,
                client_next=(sequence + 1) & 0xFFFFFFFF,
                server_isn=server_isn,
                server_next=(server_isn + 1) & 0xFFFFFFFF,
                server_acked=server_isn,
                peer_window=max(1, window),
                peer_mss=self._tcp_mss(segment[20:header_length]),
                pipe=pipe,
            )
            self.tcp = connection
            self._send_tcp(
                connection,
                server_isn,
                connection.client_next,
                self.TCP_SYN | self.TCP_ACK,
                options=b"\x02\x04" + struct.pack("!H", RNDIS_TCP_MSS),
            )
            return None

        if not matches or connection is None:
            self._send_tcp_reset(
                source_mac,
                source_ip,
                source_port,
                destination_port,
                sequence,
                acknowledgement,
                flags,
                len(payload),
            )
            return None
        if flags & self.TCP_RST:
            return self._detach_tcp_locked(connection)

        connection.peer_window = max(0, window)
        if flags & self.TCP_ACK:
            if connection.server_acked <= acknowledgement <= connection.server_next:
                connection.server_acked = acknowledgement
            elif acknowledgement > connection.server_next:
                self._send_tcp(
                    connection,
                    connection.server_next,
                    connection.client_next,
                    self.TCP_ACK,
                )
                return None
            if connection.state == "syn-received" and acknowledgement == connection.server_next:
                connection.state = "established"
            elif connection.state == "last-ack" and acknowledgement == connection.server_next:
                return self._detach_tcp_locked(connection)
            elif connection.state == "fin-wait-1" and acknowledgement == connection.server_next:
                connection.state = "fin-wait-2"

        if connection.state == "syn-received":
            return None
        if sequence != connection.client_next:
            self._send_tcp(
                connection,
                connection.server_next,
                connection.client_next,
                self.TCP_ACK,
            )
            return None
        if payload:
            if connection.state != "established":
                self._send_tcp_reset(
                    source_mac,
                    source_ip,
                    source_port,
                    destination_port,
                    sequence,
                    acknowledgement,
                    flags,
                    len(payload),
                )
                return self._detach_tcp_locked(connection)
            try:
                connection.pipe.feed(payload)  # type: ignore[attr-defined]
            except USBProtocolError:
                self._send_tcp_reset(
                    source_mac,
                    source_ip,
                    source_port,
                    destination_port,
                    sequence,
                    acknowledgement,
                    flags,
                    len(payload),
                )
                return self._detach_tcp_locked(connection)
            connection.client_next = (connection.client_next + len(payload)) & 0xFFFFFFFF
            self._send_tcp(
                connection,
                connection.server_next,
                connection.client_next,
                self.TCP_ACK,
            )
            self._drain_backend_locked(connection)

        if flags & self.TCP_FIN and connection.state in ("fin-wait-1", "fin-wait-2"):
            connection.client_next = (connection.client_next + 1) & 0xFFFFFFFF
            self._send_tcp(
                connection,
                connection.server_next,
                connection.client_next,
                self.TCP_ACK,
            )
            return self._detach_tcp_locked(connection)

        if flags & self.TCP_FIN:
            connection.client_next = (connection.client_next + 1) & 0xFFFFFFFF
            self._send_tcp(
                connection,
                connection.server_next,
                connection.client_next,
                self.TCP_ACK,
            )
            self._drain_backend_locked(connection)
            if not connection.fin_sent:
                self._send_tcp(
                    connection,
                    connection.server_next,
                    connection.client_next,
                    self.TCP_FIN | self.TCP_ACK,
                )
                connection.server_next = (connection.server_next + 1) & 0xFFFFFFFF
                connection.fin_sent = True
            connection.state = "last-ack"
            return None

        if not payload and connection.state == "established":
            self._drain_backend_locked(connection)
        return None

    def _drain_backend_locked(self, connection: _RNDISTCPConnection) -> None:
        if connection.state != "established" or connection.backend_closed:
            return
        while True:
            allowed = connection.server_acked + connection.peer_window - connection.server_next
            queue_room = self.rndis.available_output_bytes() - 98
            if allowed <= 0 or queue_room <= 0:
                return
            maximum = min(connection.peer_mss, RNDIS_TCP_MSS, allowed, queue_room)
            try:
                data = connection.pipe.read(maximum)  # type: ignore[attr-defined]
            except USBProtocolError:
                connection.backend_closed = True
                if not connection.fin_sent:
                    self._send_tcp(
                        connection,
                        connection.server_next,
                        connection.client_next,
                        self.TCP_FIN | self.TCP_ACK,
                    )
                    connection.server_next = (connection.server_next + 1) & 0xFFFFFFFF
                    connection.fin_sent = True
                    connection.state = "fin-wait-1"
                return
            if not data:
                return
            self._send_tcp(
                connection,
                connection.server_next,
                connection.client_next,
                self.TCP_PSH | self.TCP_ACK,
                payload=data,
            )
            connection.server_next = (connection.server_next + len(data)) & 0xFFFFFFFF


@dataclass(frozen=True)
class USBIPDeviceInfo:
    path: str = "/sys/devices/platform/neptune-twin/usb1/1-1"
    busid: str = "1-1"
    busnum: int = 1
    devnum: int = 1
    speed: int = USBIP_SPEED_HIGH


@dataclass
class _PendingIN:
    sequence: int
    endpoint: int
    maximum: int


class _CompositeDataPlane:
    def __init__(
        self,
        gadget: USBControlEndpoint,
        pipe_factory: Callable[[Callable[[], None]], object],
        data_ready: Callable[[], None],
        rndis_device_ip: str,
        rndis_host_ip: str,
    ) -> None:
        self.gadget = gadget
        self.pipe_factory = pipe_factory
        self.data_ready = data_ready
        self.iio_pipes: Dict[int, object] = {}
        self.mass_storage = _MassStorageBOT()
        self.console = _CDCConsole()
        self.rndis = _RNDIS(
            _mac_bytes(gadget.mac_addresses.host),
            _mac_bytes(gadget.mac_addresses.device),
        )
        self.rndis_network = _RNDISNetwork(
            self.rndis,
            pipe_factory,
            data_ready,
            rndis_device_ip,
            rndis_host_ip,
        )
        self.address = 0
        self.alternate_settings: Dict[int, int] = defaultdict(int)
        self.halted_endpoints = set()
        configuration = gadget.profile.parsed_configuration
        self.valid_interfaces = set(range(configuration.declared_interface_count))
        self.valid_endpoints = set(configuration.endpoint_addresses)

    def _close_iio_pipes(self) -> None:
        pipes = tuple(self.iio_pipes.values())
        self.iio_pipes.clear()
        for pipe in pipes:
            pipe.close()  # type: ignore[attr-defined]

    def reset(self) -> None:
        self.gadget.reset()
        self._close_iio_pipes()
        self.rndis_network.reset()
        self.rndis.reset()
        self.mass_storage.reset()
        self.console.reset_notifications()
        self.address = 0
        self.alternate_settings.clear()
        self.halted_endpoints.clear()

    close = reset

    def control(self, setup_bytes: bytes, payload: bytes) -> bytes:
        setup = SetupPacket.from_bytes(setup_bytes)
        request_type = setup.request_type
        type_bits = request_type & 0x60
        recipient = request_type & 0x1F

        if type_bits == USB_TYPE_STANDARD:
            if recipient == USB_RECIP_DEVICE and setup.request == USB_REQ_SET_ADDRESS:
                if (
                    setup.request_type != 0x00
                    or setup.index
                    or setup.length
                    or payload
                    or setup.value > 127
                    or self.gadget.configured
                ):
                    raise USBProtocolError("malformed SET_ADDRESS")
                self.address = setup.value
                return b""
            if setup.request == USB_REQ_GET_STATUS and setup.direction_in:
                if setup.value or setup.length != 2 or payload:
                    raise USBProtocolError("malformed GET_STATUS")
                if recipient == USB_RECIP_DEVICE:
                    if setup.request_type != 0x80 or setup.index:
                        raise USBProtocolError("malformed device GET_STATUS")
                    status = 0
                elif recipient == USB_RECIP_INTERFACE:
                    if (
                        setup.request_type != 0x81
                        or setup.index not in self.valid_interfaces
                        or not self.gadget.configured
                    ):
                        raise USBProtocolError("malformed interface GET_STATUS")
                    status = 0
                elif recipient == USB_RECIP_ENDPOINT:
                    if (
                        setup.request_type != 0x82
                        or setup.index & 0xFF00
                        or setup.index not in self.valid_endpoints | {0}
                    ):
                        raise USBProtocolError("malformed endpoint GET_STATUS")
                    status = int(setup.index in self.halted_endpoints)
                else:
                    raise USBProtocolError("unsupported GET_STATUS recipient")
                return struct.pack("<H", status)
            if setup.request in (USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE) and recipient == USB_RECIP_ENDPOINT:
                endpoint = setup.index
                if (
                    setup.request_type != 0x02
                    or setup.index & 0xFF00
                    or endpoint not in self.valid_endpoints
                    or setup.value != 0
                    or setup.length
                    or payload
                ):
                    raise USBProtocolError("unsupported endpoint feature")
                if setup.request == USB_REQ_SET_FEATURE:
                    self.halted_endpoints.add(endpoint)
                else:
                    self.halted_endpoints.discard(endpoint)
                return b""
            if setup.request == USB_REQ_GET_INTERFACE and setup.direction_in:
                if (
                    setup.request_type != 0x81
                    or setup.value
                    or setup.length != 1
                    or setup.index not in self.valid_interfaces
                    or not self.gadget.configured
                    or payload
                ):
                    raise USBProtocolError("malformed GET_INTERFACE")
                return bytes((self.alternate_settings[setup.index],))[:setup.length]
            if setup.request == USB_REQ_SET_INTERFACE and not setup.direction_in:
                if (
                    setup.request_type != 0x01
                    or setup.index not in self.valid_interfaces
                    or not self.gadget.configured
                    or setup.value != 0
                    or setup.length
                    or payload
                ):
                    raise USBProtocolError("only alternate setting zero exists")
                self.alternate_settings[setup.index] = 0
                return b""
            result = self.gadget.control_transfer(setup, payload)
            if setup.request_type == 0x00 and setup.request == 0x09:
                self._close_iio_pipes()
                self.rndis_network.reset()
                self.rndis.reset()
                self.mass_storage.reset()
                self.console.reset_notifications()
                self.alternate_settings.clear()
                self.halted_endpoints.clear()
            return result

        if type_bits == USB_TYPE_CLASS and recipient == USB_RECIP_INTERFACE:
            if not self.gadget.configured:
                raise USBProtocolError("USB class request received while unconfigured")
            interface = setup.index & 0xFF
            if interface == 0:
                if setup.request == CDC_SEND_ENCAPSULATED_COMMAND and not setup.direction_in:
                    if setup.request_type != 0x21 or setup.value or setup.index != 0:
                        raise USBProtocolError("malformed RNDIS encapsulated command")
                    self.rndis.control_command(payload)
                    kind = struct.unpack_from("<I", payload)[0]
                    if kind in (RNDIS_HALT_MSG, RNDIS_RESET_MSG):
                        self.rndis_network.reset()
                    return b""
                if setup.request == CDC_GET_ENCAPSULATED_RESPONSE and setup.direction_in:
                    if setup.request_type != 0xA1 or setup.value or setup.index != 0 or payload:
                        raise USBProtocolError("malformed RNDIS encapsulated response")
                    return self.rndis.control_response(setup.length)
            if interface == 2:
                if setup.request == MSC_GET_MAX_LUN and setup.direction_in:
                    if (
                        setup.request_type != 0xA1
                        or setup.value
                        or setup.index != 2
                        or setup.length != 1
                        or payload
                    ):
                        raise USBProtocolError("malformed GET_MAX_LUN")
                    return b"\0"[:setup.length]
                if setup.request == MSC_BULK_ONLY_RESET and not setup.direction_in:
                    if (
                        setup.request_type != 0x21
                        or setup.value
                        or setup.index != 2
                        or setup.length
                        or payload
                    ):
                        raise USBProtocolError("malformed mass-storage reset")
                    self.mass_storage.reset()
                    return b""
            if interface == 3:
                if (
                    setup.request == CDC_SET_LINE_CODING
                    and not setup.direction_in
                    and setup.request_type == 0x21
                    and not setup.value
                    and setup.index == 3
                    and setup.length == 7
                    and len(payload) == 7
                ):
                    self.console.line_coding[:] = payload
                    return b""
                if setup.request == CDC_GET_LINE_CODING and setup.direction_in:
                    if (
                        setup.request_type != 0xA1
                        or setup.value
                        or setup.index != 3
                        or setup.length != 7
                        or payload
                    ):
                        raise USBProtocolError("malformed CDC GET_LINE_CODING")
                    return bytes(self.console.line_coding)[:setup.length]
                if setup.request == CDC_SET_CONTROL_LINE_STATE and not setup.direction_in:
                    if (
                        setup.request_type != 0x21
                        or setup.index != 3
                        or setup.length
                        or payload
                    ):
                        raise USBProtocolError("malformed CDC control-line state")
                    self.console.control_line_state = setup.value
                    self.console.queue_serial_state()
                    return b""
                if setup.request == CDC_SEND_BREAK and not setup.direction_in:
                    if (
                        setup.request_type != 0x21
                        or setup.index != 3
                        or setup.length
                        or payload
                    ):
                        raise USBProtocolError("malformed CDC break request")
                    return b""
            raise USBProtocolError("unsupported USB class request")

        result = self.gadget.control_transfer(setup, payload)
        if (
            type_bits == 0x40
            and recipient == USB_RECIP_INTERFACE
            and setup.index == self.gadget.profile.iio_interface
        ):
            if setup.request == IIO_REQ_RESET_PIPES:
                self._close_iio_pipes()
            elif setup.request == IIO_REQ_OPEN_PIPE:
                previous = self.iio_pipes.pop(setup.value, None)
                if previous is not None:
                    previous.close()  # type: ignore[attr-defined]
                try:
                    self.iio_pipes[setup.value] = self.pipe_factory(self.data_ready)
                except USBProtocolError:
                    self.gadget.open_pipes.discard(setup.value)
                    raise
            elif setup.request == IIO_REQ_CLOSE_PIPE:
                pipe = self.iio_pipes.pop(setup.value, None)
                if pipe is not None:
                    pipe.close()  # type: ignore[attr-defined]
        return result

    def bulk_out(self, endpoint: int, data: bytes) -> None:
        if not self.gadget.configured:
            raise USBProtocolError("bulk transfer received while USB is unconfigured")
        if endpoint not in self.valid_endpoints:
            raise USBProtocolError("unsupported USB bulk OUT endpoint")
        if endpoint in self.halted_endpoints:
            raise USBProtocolError("endpoint is halted")
        if endpoint == 1:
            for frame in self.rndis.feed_packets(data):
                if self.rndis.packet_filter:
                    self.rndis_network.handle_frame(frame)
            return
        if endpoint == 2:
            self.mass_storage.feed(data)
            return
        if endpoint == 3:
            self.console.feed(data)
            return
        if endpoint in (4, 5, 6):
            pipe = endpoint - 4
            if pipe not in self.gadget.open_pipes or pipe not in self.iio_pipes:
                raise USBProtocolError("native-IIO pipe is closed")
            self.iio_pipes[pipe].feed(data)  # type: ignore[attr-defined]
            return
        raise USBProtocolError("unsupported USB bulk OUT endpoint")

    def bulk_in(self, endpoint: int, maximum: int) -> Optional[bytes]:
        address = 0x80 | endpoint
        if not self.gadget.configured:
            raise USBProtocolError("bulk transfer received while USB is unconfigured")
        if address not in self.valid_endpoints:
            raise USBProtocolError("unsupported USB bulk IN endpoint")
        if address in self.halted_endpoints:
            raise USBProtocolError("endpoint is halted")
        if endpoint == 1:
            data = self.rndis.read_packets(maximum)
            if data:
                self.rndis_network.on_usb_drain()
        elif endpoint == 2:
            # RNDIS notification: RESPONSE_AVAILABLE.
            data = self.rndis.take_control_notification(maximum)
        elif endpoint == 3:
            data = self.mass_storage.read(maximum)
        elif endpoint == 4:
            data = self.console.read(maximum)
        elif endpoint == 5:
            data = self.console.read_notification(maximum)
        elif endpoint in (6, 7, 8):
            pipe = endpoint - 6
            if pipe not in self.gadget.open_pipes or pipe not in self.iio_pipes:
                raise USBProtocolError("native-IIO pipe is closed")
            data = self.iio_pipes[pipe].read(maximum)  # type: ignore[attr-defined]
        else:
            raise USBProtocolError("unsupported USB bulk IN endpoint")
        return data if data else None


class _USBIPImportConnection:
    def __init__(self, server: "USBIPServer", reader, writer) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.send_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.flush_lock = threading.Lock()
        self.pending: Dict[int, _PendingIN] = {}
        self.pending_by_endpoint: Dict[int, Deque[int]] = defaultdict(deque)
        self.data_plane = _CompositeDataPlane(
            server.gadget,
            server.create_iio_pipe,
            self._flush_all,
            server.rndis_device_ip,
            server.rndis_host_ip,
        )

    def run(self) -> None:
        self.data_plane.reset()
        try:
            while True:
                basic = _read_exact(self.reader, _BASIC_HEADER.size)
                command, sequence, devid, direction, endpoint = _BASIC_HEADER.unpack(basic)
                if devid != (self.server.info.busnum << 16 | self.server.info.devnum):
                    raise USBProtocolError("USB/IP request has the wrong device id")
                if command == USBIP_CMD_SUBMIT:
                    body = _read_exact(self.reader, _SUBMIT_BODY.size)
                    flags, length, start_frame, packet_count, interval, setup = _SUBMIT_BODY.unpack(body)
                    invalid = (
                        direction not in (USBIP_DIR_OUT, USBIP_DIR_IN)
                        or endpoint > 15
                        or length < 0
                        or length > MAX_URB_BYTES
                        or packet_count not in (0, -1)
                    )
                    if invalid:
                        self._send_submit(sequence, -errno.EINVAL, b"")
                        if direction == USBIP_DIR_OUT and 0 <= length <= MAX_URB_BYTES:
                            _read_exact(self.reader, length)
                        if direction not in (USBIP_DIR_OUT, USBIP_DIR_IN) or not 0 <= length <= MAX_URB_BYTES:
                            raise USBProtocolError("USB/IP submit header cannot be resynchronized")
                        continue
                    payload = _read_exact(self.reader, length) if direction == USBIP_DIR_OUT else b""
                    self.server._record("submits")
                    self._submit(sequence, direction, endpoint, length, setup, payload)
                elif command == USBIP_CMD_UNLINK:
                    body = _read_exact(self.reader, _UNLINK_BODY.size)
                    target, padding = _UNLINK_BODY.unpack(body)
                    if direction or endpoint or padding != b"\0" * 24:
                        raise USBProtocolError("malformed USB/IP unlink request")
                    self._unlink(sequence, target)
                else:
                    raise USBProtocolError("unknown USB/IP URB command")
        finally:
            with self.state_lock:
                self.pending.clear()
                self.pending_by_endpoint.clear()
            self.data_plane.close()

    def _submit(
        self,
        sequence: int,
        direction: int,
        endpoint: int,
        length: int,
        setup: bytes,
        payload: bytes,
    ) -> None:
        try:
            if endpoint == 0:
                control_setup = SetupPacket.from_bytes(setup)
                expected_direction = (
                    USBIP_DIR_IN if control_setup.direction_in else USBIP_DIR_OUT
                )
                if direction != expected_direction or length != control_setup.length:
                    raise USBProtocolError(
                        "USB/IP control header disagrees with the setup packet"
                    )
                result = self.data_plane.control(setup, payload)
                self._send_submit(sequence, 0, result[:length] if direction == USBIP_DIR_IN else b"",
                                  actual_out=len(payload) if direction == USBIP_DIR_OUT else None)
                self._flush_all()
                return
            if direction == USBIP_DIR_OUT:
                self.data_plane.bulk_out(endpoint, payload)
                self._send_submit(sequence, 0, b"", actual_out=len(payload))
                self._flush_all()
                return
            if direction != USBIP_DIR_IN:
                raise USBProtocolError("invalid USB/IP direction")
            result = self.data_plane.bulk_in(endpoint, length)
            if length == 0:
                self._send_submit(sequence, 0, b"")
                return
            if result is None:
                with self.state_lock:
                    if len(self.pending) >= MAX_PENDING_IN_URBS:
                        self._send_submit(sequence, -errno.ENOMEM, b"")
                        return
                    self.pending[sequence] = _PendingIN(sequence, endpoint, length)
                    self.pending_by_endpoint[endpoint].append(sequence)
                # Close the race where a backend made data available after the
                # first read but before this pending request was registered.
                self._flush_all()
                return
            self._send_submit(sequence, 0, result)
        except USBProtocolError:
            self.server._record("stalls")
            self._send_submit(sequence, -errno.EPIPE, b"")

    def _flush_all(self) -> None:
        with self.flush_lock:
            for endpoint in tuple(self.pending_by_endpoint):
                while True:
                    with self.state_lock:
                        queue = self.pending_by_endpoint[endpoint]
                        while queue and queue[0] not in self.pending:
                            queue.popleft()
                        if not queue:
                            self.pending_by_endpoint.pop(endpoint, None)
                            break
                        pending = self.pending[queue[0]]
                    try:
                        result = self.data_plane.bulk_in(endpoint, pending.maximum)
                    except USBProtocolError:
                        result = b""
                        status = -errno.EPIPE
                    else:
                        if result is None:
                            break
                        status = 0
                    with self.state_lock:
                        if not queue or queue[0] != pending.sequence:
                            continue
                        queue.popleft()
                        if self.pending.pop(pending.sequence, None) is None:
                            continue
                    self._send_submit(pending.sequence, status, result)

    def _unlink(self, sequence: int, target: int) -> None:
        with self.state_lock:
            pending = self.pending.pop(target, None)
            existed = pending is not None
            if pending is not None:
                queue = self.pending_by_endpoint.get(pending.endpoint)
                if queue is not None:
                    try:
                        queue.remove(target)
                    except ValueError:
                        pass
                    if not queue:
                        self.pending_by_endpoint.pop(pending.endpoint, None)
        status = -errno.ECONNRESET if existed else 0
        packet = _BASIC_HEADER.pack(USBIP_RET_UNLINK, sequence, 0, 0, 0)
        packet += _RET_UNLINK_BODY.pack(status, b"\0" * 24)
        self._send(packet)
        self.server._record("unlinks")

    def _send_submit(
        self,
        sequence: int,
        status: int,
        data: bytes,
        actual_out: Optional[int] = None,
    ) -> None:
        payload = bytes(data)
        actual = len(payload) if actual_out is None else actual_out
        packet = _BASIC_HEADER.pack(USBIP_RET_SUBMIT, sequence, 0, 0, 0)
        packet += _RET_SUBMIT_BODY.pack(status, actual, 0, -1, 0, b"\0" * 8)
        packet += payload
        self._send(packet)

    def _send(self, packet: bytes) -> None:
        with self.send_lock:
            self.writer.write(packet)
            self.writer.flush()


class _USBIPRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: USBIPServer = self.server  # type: ignore[assignment]
        try:
            header = _read_exact(self.rfile, _OP_HEADER.size)
            version, operation, status = _OP_HEADER.unpack(header)
            if version != USBIP_VERSION or status:
                return
            if operation == OP_REQ_DEVLIST:
                server._record("devlist_requests")
                self.wfile.write(_OP_HEADER.pack(USBIP_VERSION, OP_REP_DEVLIST, 0))
                self.wfile.write(struct.pack("!I", 1))
                self.wfile.write(server.device_record(include_interfaces=True))
                self.wfile.flush()
                return
            if operation != OP_REQ_IMPORT:
                return
            busid_raw = _read_exact(self.rfile, 32)
            busid = busid_raw.split(b"\0", 1)[0].decode("ascii", errors="replace")
            if busid != server.info.busid or not server._claim(self.connection):
                self.wfile.write(_OP_HEADER.pack(USBIP_VERSION, OP_REP_IMPORT, 1))
                self.wfile.flush()
                return
            try:
                server._record("imports")
                self.wfile.write(_OP_HEADER.pack(USBIP_VERSION, OP_REP_IMPORT, 0))
                self.wfile.write(server.device_record(include_interfaces=False))
                self.wfile.flush()
                import_connection = _USBIPImportConnection(server, self.rfile, self.wfile)
                server._register_import(import_connection)
                try:
                    import_connection.run()
                finally:
                    server._unregister_import(import_connection)
            finally:
                server.gadget.reset()
                server._release(self.connection)
        except (EOFError, ConnectionError, OSError, USBProtocolError):
            return


class USBIPServer(socketserver.ThreadingTCPServer):
    """Export one NeptuneSDR composite USB device through USB/IP v1.1.1."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        gadget: USBControlEndpoint,
        context: Optional[IIOContext] = None,
        host: str = "127.0.0.1",
        port: int = USBIP_PORT,
        info: USBIPDeviceInfo = USBIPDeviceInfo(),
        iiod_backend: Optional[Tuple[str, int]] = None,
        rndis_device_ip: str = RNDIS_DEVICE_IP,
        rndis_host_ip: str = RNDIS_HOST_IP,
    ) -> None:
        if (context is None) == (iiod_backend is None):
            raise ValueError("select exactly one local IIO context or TCP IIOD backend")
        self.gadget = gadget
        self.context = context
        self.iiod_backend = (
            (str(iiod_backend[0]), int(iiod_backend[1]))
            if iiod_backend is not None
            else None
        )
        self.info = info
        self.rndis_device_ip = str(ipaddress.IPv4Address(rndis_device_ip))
        self.rndis_host_ip = str(ipaddress.IPv4Address(rndis_host_ip))
        if (
            self.rndis_device_ip == self.rndis_host_ip
            or self.rndis_device_ip.rsplit(".", 1)[0]
            != self.rndis_host_ip.rsplit(".", 1)[0]
        ):
            raise ValueError("RNDIS device and host addresses must be distinct in one /24")
        self._thread: Optional[threading.Thread] = None
        self._attached = False
        self._state_lock = threading.Lock()
        self._active_connections = set()
        self._active_import: Optional[_USBIPImportConnection] = None
        self._released = threading.Event()
        self._released.set()
        self._counters = {
            "devlist_requests": 0,
            "imports": 0,
            "submits": 0,
            "unlinks": 0,
            "stalls": 0,
        }
        super().__init__((host, int(port)), _USBIPRequestHandler)

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self.server_address
        return str(host), int(port)

    def start(self) -> "USBIPServer":
        if self._thread is not None:
            raise RuntimeError("USB/IP server already started")
        self._thread = threading.Thread(
            target=self.serve_forever, name="p210-usbip", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            with self._state_lock:
                connections = tuple(self._active_connections)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            if connections:
                self._released.wait(timeout=2.0)
            self.shutdown()
            self._thread.join(timeout=2.0)
            self._thread = None
        self.server_close()

    def __enter__(self) -> "USBIPServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()

    def _claim(self, connection: socket.socket) -> bool:
        with self._state_lock:
            if self._attached:
                return False
            self._attached = True
            self._active_connections.add(connection)
            self._released.clear()
            return True

    def _release(self, connection: socket.socket) -> None:
        with self._state_lock:
            self._active_connections.discard(connection)
            self._attached = False
            self._released.set()

    def _register_import(self, connection: _USBIPImportConnection) -> None:
        with self._state_lock:
            self._active_import = connection

    def _unregister_import(self, connection: _USBIPImportConnection) -> None:
        with self._state_lock:
            if self._active_import is connection:
                self._active_import = None

    def _record(self, name: str) -> None:
        with self._state_lock:
            self._counters[name] += 1

    def create_iio_pipe(self, data_ready: Callable[[], None]):
        if self.context is not None:
            return _IIODBytePipe(self.context)
        assert self.iiod_backend is not None
        return _SocketIIODBytePipe(self.iiod_backend, data_ready)

    def snapshot(self) -> Mapping[str, object]:
        with self._state_lock:
            counters = dict(self._counters)
            attached = self._attached
            active_import = self._active_import
        rndis = {
            "device_mac": self.gadget.mac_addresses.device,
            "device_ip": self.rndis_device_ip,
            "host_ip": self.rndis_host_ip,
            "subnet_mask": "255.255.255.0",
            "iiod_port": RNDIS_IIOD_PORT,
            "tcp_state": "closed",
        }
        if active_import is not None:
            rndis.update(active_import.data_plane.rndis_network.snapshot())
        return {
            "running": self._thread is not None,
            "address": list(self.address),
            "version": USBIP_VERSION,
            "busid": self.info.busid,
            "attached": attached,
            "iiod_backend": list(self.iiod_backend) if self.iiod_backend else "local",
            "rndis": rndis,
            "counters": counters,
        }

    def device_record(self, *, include_interfaces: bool) -> bytes:
        device = self.gadget.profile.parsed_device
        config = self.gadget.profile.parsed_configuration
        record = bytearray()
        record.extend(_padded_ascii(self.info.path, 256))
        record.extend(_padded_ascii(self.info.busid, 32))
        record.extend(_DEVICE_FIXED.pack(
            self.info.busnum,
            self.info.devnum,
            self.info.speed,
            device.vendor_id,
            device.product_id,
            device.device_version,
            device.device_class,
            device.device_subclass,
            device.device_protocol,
            config.configuration_value,
            device.configuration_count,
            config.declared_interface_count,
        ))
        if include_interfaces:
            for number in range(config.declared_interface_count):
                interface = config.interface(number)
                record.extend(_INTERFACE_INFO.pack(
                    interface.interface_class,
                    interface.interface_subclass,
                    interface.interface_protocol,
                    0,
                ))
        return bytes(record)


__all__ = [
    "OP_REP_DEVLIST",
    "OP_REP_IMPORT",
    "OP_REQ_DEVLIST",
    "OP_REQ_IMPORT",
    "RNDIS_DEVICE_IP",
    "RNDIS_HOST_IP",
    "RNDIS_IIOD_PORT",
    "USBIP_CMD_SUBMIT",
    "USBIP_CMD_UNLINK",
    "USBIP_DIR_IN",
    "USBIP_DIR_OUT",
    "USBIP_PORT",
    "USBIP_RET_SUBMIT",
    "USBIP_RET_UNLINK",
    "USBIP_VERSION",
    "USBIPDeviceInfo",
    "USBIPServer",
    "build_read_only_volume",
]
