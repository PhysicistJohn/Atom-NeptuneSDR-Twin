"""End-to-end checks for the host-visible NeptuneSDR USB composite device."""

import errno
import socket
import struct
import unittest

from neptunesdr_twin import NeptuneSDRTwin
from neptunesdr_twin.usb import SetupPacket
from neptunesdr_twin.usbip import (
    OP_REP_DEVLIST,
    OP_REP_IMPORT,
    OP_REQ_DEVLIST,
    OP_REQ_IMPORT,
    USBIP_CMD_SUBMIT,
    USBIP_DIR_IN,
    USBIP_DIR_OUT,
    USBIP_RET_SUBMIT,
    USBIP_VERSION,
    USBIPServer,
)


_OP_HEADER = struct.Struct("!HHI")
_BASIC = struct.Struct("!IIIII")
_SUBMIT = struct.Struct("!Iiiii8s")
_RET_SUBMIT = struct.Struct("!iiiii8s")
_CBW = struct.Struct("<4sIIBBB16s")
_CSW = struct.Struct("<4sIIB")
_HOST_MAC = bytes.fromhex("02 00 00 00 02 0a")
_HOST_IP = socket.inet_aton("192.168.2.10")
_DEVICE_IP = socket.inet_aton("192.168.2.1")


def _receive_exact(connection, count):
    data = bytearray()
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise EOFError("USB/IP peer closed mid-packet")
        data.extend(chunk)
    return bytes(data)


class _USBIPClient:
    """Small USB/IP host: just enough protocol to exercise real URBs."""

    def __init__(self, address):
        self.connection = socket.create_connection(address, timeout=2.0)
        self.connection.settimeout(2.0)
        self.sequence = 0
        self.directions = {}
        self.connection.sendall(
            _OP_HEADER.pack(USBIP_VERSION, OP_REQ_IMPORT, 0)
            + b"1-1\0"
            + b"\0" * 28
        )
        header = _OP_HEADER.unpack(_receive_exact(self.connection, _OP_HEADER.size))
        if header != (USBIP_VERSION, OP_REP_IMPORT, 0):
            raise AssertionError("USB/IP import failed: %r" % (header,))
        self.device_record = _receive_exact(self.connection, 312)

    def close(self):
        self.connection.close()

    def transfer(self, direction, endpoint, length, *, setup=b"\0" * 8, payload=b""):
        self.sequence += 1
        sequence = self.sequence
        if direction == USBIP_DIR_OUT and len(payload) != length:
            raise AssertionError("USB/IP OUT length mismatch")
        request = _BASIC.pack(
            USBIP_CMD_SUBMIT, sequence, 0x00010001, direction, endpoint
        )
        request += _SUBMIT.pack(0, length, 0, -1, 0, setup)
        if direction == USBIP_DIR_OUT:
            request += payload
        self.directions[sequence] = direction
        self.connection.sendall(request)

        command, returned, _devid, _direction, _endpoint = _BASIC.unpack(
            _receive_exact(self.connection, _BASIC.size)
        )
        if (command, returned) != (USBIP_RET_SUBMIT, sequence):
            raise AssertionError("USB/IP completion did not match its URB")
        status, actual, _start, _packets, _errors, padding = _RET_SUBMIT.unpack(
            _receive_exact(self.connection, _RET_SUBMIT.size)
        )
        if padding != b"\0" * 8:
            raise AssertionError("non-zero USB/IP completion padding")
        data = (
            _receive_exact(self.connection, actual)
            if status == 0 and actual and direction == USBIP_DIR_IN
            else b""
        )
        self.directions.pop(sequence, None)
        return status, actual, data

    def control(self, setup, payload=b""):
        direction = USBIP_DIR_IN if setup.direction_in else USBIP_DIR_OUT
        return self.transfer(
            direction,
            0,
            setup.length,
            setup=setup.to_bytes(),
            payload=payload,
        )


def _checksum(data):
    payload = bytes(data)
    if len(payload) & 1:
        payload += b"\0"
    total = sum(struct.unpack("!%dH" % (len(payload) // 2), payload))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _transport_checksum(source, destination, protocol, payload):
    pseudo = source + destination + struct.pack("!BBH", 0, protocol, len(payload))
    return _checksum(pseudo + payload) or 0xFFFF


def _ipv4(source, destination, protocol, payload):
    header = bytearray(
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(payload),
            1,
            0x4000,
            64,
            protocol,
            0,
            source,
            destination,
        )
    )
    struct.pack_into("!H", header, 10, _checksum(header))
    return bytes(header) + payload


def _tcp(source_port, destination_port, sequence, acknowledgement, flags, payload=b""):
    segment = bytearray(
        struct.pack(
            "!HHIIBBHHH",
            source_port,
            destination_port,
            sequence,
            acknowledgement,
            5 << 4,
            flags,
            65535,
            0,
            0,
        )
        + payload
    )
    struct.pack_into(
        "!H", segment, 16, _transport_checksum(_HOST_IP, _DEVICE_IP, 6, segment)
    )
    return bytes(segment)


def _rndis(frame):
    return struct.pack(
        "<11I", 1, 44 + len(frame), 36, len(frame), 0, 0, 0, 0, 0, 0, 0
    ) + frame


def _decode_rndis(messages):
    frames = []
    offset = 0
    while offset < len(messages):
        kind, length, data_offset, data_length = struct.unpack_from(
            "<4I", messages, offset
        )
        if kind != 1 or length < 44 or offset + length > len(messages):
            raise AssertionError("malformed RNDIS output")
        start = offset + 8 + data_offset
        frames.append(messages[start : start + data_length])
        offset += length
    return frames


def _parse_tcp(frame):
    if frame[12:14] != b"\x08\x00":
        raise AssertionError("expected IPv4 Ethernet frame")
    packet = frame[14:]
    header_bytes = (packet[0] & 0x0F) * 4
    segment = packet[header_bytes : struct.unpack_from("!H", packet, 2)[0]]
    if packet[9] != 6 or _transport_checksum(packet[12:16], packet[16:20], 6, segment) != 0xFFFF:
        raise AssertionError("invalid TCP response")
    source_port, destination_port, sequence, acknowledgement = struct.unpack_from(
        "!HHII", segment
    )
    tcp_header_bytes = (segment[12] >> 4) * 4
    return {
        "source_port": source_port,
        "destination_port": destination_port,
        "sequence": sequence,
        "acknowledgement": acknowledgement,
        "flags": segment[13],
        "payload": segment[tcp_header_bytes:],
    }


class USBCompositeContactTests(unittest.TestCase):
    def setUp(self):
        self.twin = NeptuneSDRTwin(serial="USB-CONTACT-TEST")
        self.server = USBIPServer(self.twin.usb, self.twin.iio, port=0).start()

    def tearDown(self):
        self.server.stop()
        self.twin.close()

    def _configured_client(self):
        client = _USBIPClient(self.server.address)
        self.assertEqual(
            client.control(SetupPacket(0x00, 9, 1, 0, 0))[:2],
            (0, 0),
        )
        return client

    def _enable_rndis(self, client):
        initialize = struct.pack("<6I", 2, 24, 7, 1, 0, 0x4000)
        self.assertEqual(
            client.control(
                SetupPacket(0x21, 0, 0, 0, len(initialize)), initialize
            )[:2],
            (0, len(initialize)),
        )
        response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
        self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000002)
        packet_filter = struct.pack("<I", 0x0F)
        request = struct.pack(
            "<7I", 5, 28 + len(packet_filter), 8, 0x0001010E,
            len(packet_filter), 20, 0,
        ) + packet_filter
        client.control(SetupPacket(0x21, 0, 0, 0, len(request)), request)
        response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
        self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000005)

    def _send_frame(self, client, frame):
        message = _rndis(frame)
        self.assertEqual(
            client.transfer(USBIP_DIR_OUT, 1, len(message), payload=message)[:2],
            (0, len(message)),
        )

    def _receive_frames(self, client):
        status, actual, messages = client.transfer(USBIP_DIR_IN, 1, 65536)
        self.assertEqual((status, actual), (0, len(messages)))
        return _decode_rndis(messages)

    def test_kernel_records_enumerate_and_native_iio_controls_same_context(self):
        with socket.create_connection(self.server.address, timeout=2.0) as connection:
            connection.sendall(_OP_HEADER.pack(USBIP_VERSION, OP_REQ_DEVLIST, 0))
            self.assertEqual(
                _OP_HEADER.unpack(_receive_exact(connection, 8)),
                (USBIP_VERSION, OP_REP_DEVLIST, 0),
            )
            self.assertEqual(struct.unpack("!I", _receive_exact(connection, 4))[0], 1)
            record = _receive_exact(connection, 312 + 6 * 4)
            self.assertEqual(record[256:259], b"1-1")
            self.assertEqual(struct.unpack_from("!HH", record, 300), (0x0456, 0xB673))
            self.assertEqual(record[311], 6)

        client = _USBIPClient(self.server.address)
        try:
            status, actual, device = client.control(SetupPacket(0x80, 6, 0x0100, 0, 18))
            self.assertEqual((status, actual, device[8:12]), (0, 18, b"\x56\x04\x73\xb6"))
            self.assertEqual(client.control(SetupPacket(0x00, 9, 1, 0, 0))[:2], (0, 0))
            self.assertEqual(client.control(SetupPacket(0x41, 1, 0, 5, 0))[:2], (0, 0))

            new_lo = self.twin.radio.tx_lo_hz + 4_000_000
            value = str(new_lo).encode("ascii")
            command = b"WRITE iio:device0 OUTPUT altvoltage1 frequency %d\n" % len(value)
            for part in (command[:17], command[17:] + value[:3], value[3:]):
                self.assertEqual(
                    client.transfer(USBIP_DIR_OUT, 4, len(part), payload=part)[:2],
                    (0, len(part)),
                )
            self.assertEqual(client.transfer(USBIP_DIR_IN, 6, 64)[2], b"%d\n" % len(value))
            self.assertEqual(self.twin.radio.tx_lo_hz, new_lo)

            client.transfer(USBIP_DIR_OUT, 4, 6, payload=b"PRINT\n")
            printed = client.transfer(USBIP_DIR_IN, 6, 64 * 1024)[2]
            self.assertIn(b"<?xml", printed)
            self.assertIn(b"ad9361-phy", printed)
            self.assertIn(b"cf-ad9361-lpc", printed)
        finally:
            client.close()

    def test_mass_storage_is_read_only_and_cdc_reports_fft_contract(self):
        client = self._configured_client()
        try:
            self.assertEqual(
                client.control(SetupPacket(0xA1, 0xFE, 0, 2, 1)),
                (0, 1, b"\0"),
            )
            inquiry = b"\x12\0\0\0\x24\0".ljust(16, b"\0")
            cbw = _CBW.pack(b"USBC", 0x1234, 36, 0x80, 0, 6, inquiry)
            client.transfer(USBIP_DIR_OUT, 2, len(cbw), payload=cbw)
            response = client.transfer(USBIP_DIR_IN, 3, 64)[2]
            self.assertEqual(response[8:16], b"NEPTUNE ")
            self.assertEqual(_CSW.unpack(response[36:]), (b"USBS", 0x1234, 0, 0))

            write10 = (b"\x2a\0" + b"\0" * 8).ljust(16, b"\0")
            cbw = _CBW.pack(b"USBC", 0x5678, 512, 0x00, 0, 10, write10)
            client.transfer(USBIP_DIR_OUT, 2, len(cbw), payload=cbw)
            client.transfer(USBIP_DIR_OUT, 2, 512, payload=b"x" * 512)
            self.assertEqual(
                _CSW.unpack(client.transfer(USBIP_DIR_IN, 3, _CSW.size)[2]),
                (b"USBS", 0x5678, 512, 1),
            )

            banner = client.transfer(USBIP_DIR_IN, 4, 256)[2]
            self.assertIn(b"virtual console", banner)
            client.transfer(USBIP_DIR_OUT, 3, 12, payload=b"fft-status\r\n")
            response = client.transfer(USBIP_DIR_IN, 4, 256)[2]
            self.assertIn(b"N=65536", response)
            self.assertIn(b"bandwidth=50000000", response)
        finally:
            client.close()

    def test_rndis_ethernet_and_tcp_proxy_reach_iiod(self):
        client = self._configured_client()
        device_mac = bytes.fromhex(self.twin.usb.mac_addresses.device.replace(":", " "))
        try:
            self._enable_rndis(client)

            arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
            arp += _HOST_MAC + _HOST_IP + b"\0" * 6 + _DEVICE_IP
            self._send_frame(client, b"\xff" * 6 + _HOST_MAC + b"\x08\x06" + arp)
            arp_reply = self._receive_frames(client)[0]
            self.assertEqual(arp_reply[:12], _HOST_MAC + device_mac)
            self.assertEqual(struct.unpack_from("!H", arp_reply, 20)[0], 2)
            self.assertEqual(arp_reply[28:32], _DEVICE_IP)

            source_port = 49152

            def send_tcp(sequence, acknowledgement, flags, payload=b""):
                segment = _tcp(
                    source_port, 30431, sequence, acknowledgement, flags, payload
                )
                self._send_frame(
                    client,
                    device_mac
                    + _HOST_MAC
                    + b"\x08\x00"
                    + _ipv4(_HOST_IP, _DEVICE_IP, 6, segment),
                )

            send_tcp(1000, 0, 0x02)
            syn_ack = _parse_tcp(self._receive_frames(client)[0])
            self.assertEqual(syn_ack["flags"] & 0x12, 0x12)
            client_sequence = 1001
            server_sequence = syn_ack["sequence"] + 1
            send_tcp(client_sequence, server_sequence, 0x10)
            command = b"VERSION\r\n"
            send_tcp(client_sequence, server_sequence, 0x18, command)
            replies = [_parse_tcp(frame) for frame in self._receive_frames(client)]
            self.assertEqual(
                b"".join(reply["payload"] for reply in replies),
                b"0.26.v0.26  \n",
            )
            self.assertTrue(all(reply["acknowledgement"] == client_sequence + len(command) for reply in replies))
            snapshot = self.server.snapshot()["rndis"]
            self.assertEqual(snapshot["iiod_port"], 30431)
            self.assertEqual(snapshot["stats"]["arp_replies"], 1)
            self.assertGreaterEqual(snapshot["stats"]["tcp_segments"], 2)
        finally:
            client.close()

    def test_malformed_and_unconfigured_urbs_stall_without_state_change(self):
        client = _USBIPClient(self.server.address)
        try:
            self.assertEqual(
                client.transfer(USBIP_DIR_OUT, 3, 1, payload=b"x")[:2],
                (-errno.EPIPE, 0),
            )
            # USB/IP and setup-packet directions disagree.  The request must
            # stall and must not execute SET_CONFIGURATION.
            self.assertEqual(
                client.transfer(
                    USBIP_DIR_IN,
                    0,
                    0,
                    setup=SetupPacket(0x00, 9, 1, 0, 0).to_bytes(),
                )[:2],
                (-errno.EPIPE, 0),
            )
            self.assertFalse(self.twin.usb.configured)
            self.assertEqual(client.control(SetupPacket(0x00, 9, 1, 0, 0))[:2], (0, 0))

            truncated_rndis = struct.pack("<II", 2, 8)
            self.assertEqual(
                client.control(
                    SetupPacket(0x21, 0, 0, 0, len(truncated_rndis)),
                    truncated_rndis,
                )[:2],
                (-errno.EPIPE, 0),
            )
            self.assertEqual(
                client.transfer(USBIP_DIR_IN, 6, 0)[:2],
                (-errno.EPIPE, 0),
            )
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
