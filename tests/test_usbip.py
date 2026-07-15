import errno
import socket
import struct
import threading
import unittest

from neptunesdr_twin.board import NeptuneSDRTwin
from neptunesdr_twin.iio import IIODServer
from neptunesdr_twin.usb import SetupPacket
from neptunesdr_twin.usbip import (
    OP_REP_DEVLIST,
    OP_REP_IMPORT,
    OP_REQ_DEVLIST,
    OP_REQ_IMPORT,
    USBIP_CMD_SUBMIT,
    USBIP_CMD_UNLINK,
    USBIP_DIR_IN,
    USBIP_DIR_OUT,
    USBIP_RET_SUBMIT,
    USBIP_RET_UNLINK,
    USBIP_VERSION,
    USBIPServer,
    build_read_only_volume,
)


OP_HEADER = struct.Struct("!HHI")
BASIC = struct.Struct("!IIIII")
SUBMIT = struct.Struct("!IIIII8s")
RET_SUBMIT = struct.Struct("!iiIII8s")
UNLINK = struct.Struct("!I24s")
RET_UNLINK = struct.Struct("!i24s")
CBW = struct.Struct("<4sIIBBB16s")
CSW = struct.Struct("<4sIIB")
HOST_MAC = bytes.fromhex("02 00 00 00 02 0a")
HOST_IP = socket.inet_aton("192.168.2.10")
DEVICE_IP = socket.inet_aton("192.168.2.1")


def internet_checksum(data):
    payload = bytes(data)
    if len(payload) & 1:
        payload += b"\0"
    total = sum(struct.unpack("!%dH" % (len(payload) // 2), payload))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def ipv4_packet(source, destination, protocol, payload, identifier=1):
    header = bytearray(
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(payload),
            identifier,
            0x4000,
            64,
            protocol,
            0,
            source,
            destination,
        )
    )
    struct.pack_into("!H", header, 10, internet_checksum(header))
    return bytes(header) + payload


def transport_checksum(source, destination, protocol, payload):
    pseudo = source + destination + struct.pack("!BBH", 0, protocol, len(payload))
    result = internet_checksum(pseudo + payload)
    return result or 0xFFFF


def tcp_segment(source, destination, source_port, destination_port, sequence,
                acknowledgement, flags, payload=b"", window=65535, options=b""):
    if len(options) & 3:
        options += b"\0" * (-len(options) & 3)
    header = bytearray(
        struct.pack(
            "!HHIIBBHHH",
            source_port,
            destination_port,
            sequence,
            acknowledgement,
            ((20 + len(options)) // 4) << 4,
            flags,
            window,
            0,
            0,
        )
        + options
    )
    segment = header + payload
    struct.pack_into(
        "!H", segment, 16, transport_checksum(source, destination, 6, segment)
    )
    return bytes(segment)


def rndis_packet(frame):
    return struct.pack(
        "<11I", 1, 44 + len(frame), 36, len(frame), 0, 0, 0, 0, 0, 0, 0
    ) + frame


def decode_rndis_packets(data):
    packets = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 44:
            raise AssertionError("truncated RNDIS message")
        kind, length, data_offset, data_length = struct.unpack_from(
            "<4I", data, offset
        )
        if kind != 1 or length < 44 or offset + length > len(data):
            raise AssertionError("invalid RNDIS message")
        start = offset + 8 + data_offset
        packets.append(data[start:start + data_length])
        offset += length
    return packets


def parse_ipv4(frame):
    if struct.unpack("!H", frame[12:14])[0] != 0x0800:
        raise AssertionError("not IPv4")
    packet = frame[14:]
    header_length = (packet[0] & 0x0F) * 4
    total = struct.unpack_from("!H", packet, 2)[0]
    if internet_checksum(packet[:header_length]) != 0:
        raise AssertionError("bad IPv4 checksum")
    return packet[12:16], packet[16:20], packet[9], packet[header_length:total]


def parse_tcp(frame):
    source, destination, protocol, segment = parse_ipv4(frame)
    if protocol != 6 or transport_checksum(source, destination, 6, segment) != 0xFFFF:
        raise AssertionError("bad TCP segment")
    source_port, destination_port, sequence, acknowledgement = struct.unpack_from(
        "!HHII", segment
    )
    header_length = (segment[12] >> 4) * 4
    return {
        "source_port": source_port,
        "destination_port": destination_port,
        "sequence": sequence,
        "acknowledgement": acknowledgement,
        "flags": segment[13],
        "payload": segment[header_length:],
    }


class EOFBackend:
    """Tiny TCP backend that records whether the USB proxy closes its peer."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.address = self.listener.getsockname()
        self.accepted = threading.Event()
        self.peer_closed = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        connection = None
        try:
            self.listener.settimeout(2)
            connection, _ = self.listener.accept()
            self.accepted.set()
            connection.settimeout(0.1)
            while not self.stop_event.is_set():
                try:
                    data = connection.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    self.peer_closed.set()
                    return
        except OSError:
            return
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        self.stop_event.set()
        self.listener.close()
        self.thread.join(timeout=2)


def receive_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise EOFError("connection closed")
        result.extend(data)
    return bytes(result)


class USBIPClient:
    def __init__(self, address):
        self.sock = socket.create_connection(address, timeout=2)
        self.sequence = 0
        self.directions = {}
        self.sock.sendall(
            OP_HEADER.pack(USBIP_VERSION, OP_REQ_IMPORT, 0)
            + b"1-1\0" + b"\0" * 28
        )
        version, operation, status = OP_HEADER.unpack(receive_exact(self.sock, 8))
        if (version, operation, status) != (USBIP_VERSION, OP_REP_IMPORT, 0):
            raise AssertionError((version, operation, status))
        self.device_record = receive_exact(self.sock, 312)

    def close(self):
        self.sock.close()

    def send_submit(self, direction, endpoint, length, setup=b"\0" * 8, payload=b""):
        self.sequence += 1
        sequence = self.sequence
        packet = BASIC.pack(
            USBIP_CMD_SUBMIT, sequence, 0x00010001, direction, endpoint
        )
        packet += SUBMIT.pack(0, length, 0, 0xFFFFFFFF, 0, setup)
        if direction == USBIP_DIR_OUT:
            if len(payload) != length:
                raise AssertionError("OUT payload length mismatch")
            packet += payload
        self.directions[sequence] = direction
        self.sock.sendall(packet)
        return sequence

    def receive_submit(self):
        command, sequence, devid, direction, endpoint = BASIC.unpack(
            receive_exact(self.sock, BASIC.size)
        )
        self.assert_equal(command, USBIP_RET_SUBMIT)
        status, actual, start, packets, errors, padding = RET_SUBMIT.unpack(
            receive_exact(self.sock, RET_SUBMIT.size)
        )
        submitted_direction = self.directions.pop(sequence, None)
        data = (
            receive_exact(self.sock, actual)
            if actual and status == 0 and submitted_direction == USBIP_DIR_IN
            else b""
        )
        return sequence, status, actual, data

    def transfer(self, direction, endpoint, length, setup=b"\0" * 8, payload=b""):
        expected = self.send_submit(direction, endpoint, length, setup, payload)
        sequence, status, actual, data = self.receive_submit()
        self.assert_equal(sequence, expected)
        return status, actual, data

    def control(self, setup, payload=b""):
        direction = USBIP_DIR_IN if setup.direction_in else USBIP_DIR_OUT
        length = setup.length
        return self.transfer(direction, 0, length, setup.to_bytes(), payload)

    def unlink(self, target):
        self.sequence += 1
        sequence = self.sequence
        self.sock.sendall(
            BASIC.pack(USBIP_CMD_UNLINK, sequence, 0x00010001, 0, 0)
            + UNLINK.pack(target, b"\0" * 24)
        )
        command, returned_sequence, devid, direction, endpoint = BASIC.unpack(
            receive_exact(self.sock, BASIC.size)
        )
        self.assert_equal((command, returned_sequence), (USBIP_RET_UNLINK, sequence))
        status, padding = RET_UNLINK.unpack(receive_exact(self.sock, RET_UNLINK.size))
        self.assert_equal(padding, b"\0" * 24)
        self.directions.pop(target, None)
        return status

    @staticmethod
    def assert_equal(left, right):
        if left != right:
            raise AssertionError((left, right))


class USBIPTests(unittest.TestCase):
    def setUp(self):
        self.twin = NeptuneSDRTwin(serial="USBIPTWIN00000000000000000001")
        self.server = USBIPServer(self.twin.usb, self.twin.iio, port=0).start()

    def tearDown(self):
        self.server.stop()
        self.twin.close()

    def enable_rndis(self, client):
        client.control(SetupPacket(0x00, 9, 1, 0, 0))
        initialize = struct.pack("<6I", 2, 24, 7, 1, 0, 0x4000)
        self.assertEqual(
            client.control(
                SetupPacket(0x21, 0, 0, 0, len(initialize)), initialize
            )[:2],
            (0, len(initialize)),
        )
        response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
        self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000002)
        packet_filter = struct.pack("<I", 0x0000000F)
        set_filter = struct.pack(
            "<7I", 5, 28 + len(packet_filter), 8, 0x0001010E,
            len(packet_filter), 20, 0
        ) + packet_filter
        client.control(
            SetupPacket(0x21, 0, 0, 0, len(set_filter)), set_filter
        )
        response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
        self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000005)

    def send_ethernet(self, client, frame):
        message = rndis_packet(frame)
        self.assertEqual(
            client.transfer(
                USBIP_DIR_OUT, 1, len(message), payload=message
            )[:2],
            (0, len(message)),
        )

    def receive_ethernet(self, client, maximum=65536):
        status, actual, data = client.transfer(USBIP_DIR_IN, 1, maximum)
        self.assertEqual(status, 0)
        self.assertEqual(actual, len(data))
        return decode_rndis_packets(data)

    def test_devlist_and_import_records_are_kernel_format(self):
        with socket.create_connection(self.server.address, timeout=2) as connection:
            connection.sendall(OP_HEADER.pack(USBIP_VERSION, OP_REQ_DEVLIST, 0))
            self.assertEqual(
                OP_HEADER.unpack(receive_exact(connection, 8)),
                (USBIP_VERSION, OP_REP_DEVLIST, 0),
            )
            self.assertEqual(struct.unpack("!I", receive_exact(connection, 4))[0], 1)
            record = receive_exact(connection, 312 + 6 * 4)
            self.assertEqual(record[256:259], b"1-1")
            self.assertEqual(struct.unpack_from("!H", record, 300)[0], 0x0456)
            self.assertEqual(struct.unpack_from("!H", record, 302)[0], 0xB673)
            self.assertEqual(record[311], 6)

        client = USBIPClient(self.server.address)
        try:
            self.assertEqual(client.device_record[256:259], b"1-1")
        finally:
            client.close()

    def test_enumeration_and_native_libiio_pipe(self):
        client = USBIPClient(self.server.address)
        try:
            status, actual, device = client.control(
                SetupPacket(0x80, 6, 0x0100, 0, 18)
            )
            self.assertEqual((status, actual), (0, 18))
            self.assertEqual(device[8:12], b"\x56\x04\x73\xb6")

            status, actual, config_head = client.control(
                SetupPacket(0x80, 6, 0x0200, 0, 9)
            )
            self.assertEqual((status, len(config_head)), (0, 9))
            total = struct.unpack_from("<H", config_head, 2)[0]
            status, actual, config = client.control(
                SetupPacket(0x80, 6, 0x0200, 0, total)
            )
            self.assertEqual((status, actual), (0, 218))
            self.assertEqual(config, self.twin.usb.configuration_descriptor)

            self.assertEqual(client.control(SetupPacket(0x00, 9, 1, 0, 0))[:2], (0, 0))
            self.assertEqual(client.control(SetupPacket(0x41, 1, 0, 5, 0))[:2], (0, 0))

            status, actual, data = client.transfer(
                USBIP_DIR_OUT, 4, len(b"VERSION\r\n"), payload=b"VERSION\r\n"
            )
            self.assertEqual((status, actual, data), (0, 9, b""))
            status, actual, data = client.transfer(USBIP_DIR_IN, 6, 64)
            self.assertEqual(status, 0)
            self.assertEqual(actual, len(data))
            self.assertTrue(data.startswith(b"0.26."))

            client.transfer(USBIP_DIR_OUT, 4, 6, payload=b"PRINT\n")
            status, actual, data = client.transfer(USBIP_DIR_IN, 6, 64 * 1024)
            self.assertEqual(status, 0)
            self.assertIn(b"<?xml", data)
            self.assertIn(b"cf-ad9361-lpc", data)
        finally:
            client.close()

    def test_native_iio_preserves_stream_framing_across_usb_urbs(self):
        client = USBIPClient(self.server.address)
        try:
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            client.control(SetupPacket(0x41, 1, 0, 5, 0))
            line = b"WRITE iio:device0 OUTPUT altvoltage1 frequency 10\n"
            payload = b"2500000000"
            pieces = (line[:13], line[13:] + payload[:3], payload[3:])
            for piece in pieces:
                self.assertEqual(
                    client.transfer(
                        USBIP_DIR_OUT, 4, len(piece), payload=piece
                    )[:2],
                    (0, len(piece)),
                )
            status, actual, response = client.transfer(USBIP_DIR_IN, 6, 64)
            self.assertEqual((status, response), (0, b"10\n"))
            self.assertEqual(actual, len(response))
            self.assertEqual(self.twin.radio.tx_lo_hz, 2_500_000_000)

            # WRITEBUF's handshake and binary body are two protocol phases,
            # even when each is transported by independent USB URBs.
            open_line = b"OPEN iio:device2 4 0000000f\n"
            client.transfer(
                USBIP_DIR_OUT, 4, len(open_line), payload=open_line
            )
            self.assertEqual(client.transfer(USBIP_DIR_IN, 6, 64)[2], b"0\n")
            write_line = b"WRITEBUF iio:device2 16\n"
            client.transfer(
                USBIP_DIR_OUT, 4, len(write_line), payload=write_line
            )
            self.assertEqual(client.transfer(USBIP_DIR_IN, 6, 64)[2], b"16\n")
            client.transfer(USBIP_DIR_OUT, 4, 7, payload=b"\0" * 7)
            client.transfer(USBIP_DIR_OUT, 4, 9, payload=b"\0" * 9)
            self.assertEqual(client.transfer(USBIP_DIR_IN, 6, 64)[2], b"16\n")
        finally:
            client.close()

    def test_mass_storage_cdc_and_rndis_functions(self):
        client = USBIPClient(self.server.address)
        try:
            client.control(SetupPacket(0x00, 9, 1, 0, 0))

            status, actual, data = client.control(SetupPacket(0xA1, 0xFE, 0, 2, 1))
            self.assertEqual((status, actual, data), (0, 1, b"\0"))
            cdb = b"\x12\0\0\0\x24\0".ljust(16, b"\0")
            cbw = CBW.pack(b"USBC", 0x1234, 36, 0x80, 0, 6, cdb)
            client.transfer(USBIP_DIR_OUT, 2, len(cbw), payload=cbw)
            status, actual, data = client.transfer(USBIP_DIR_IN, 3, 64)
            self.assertEqual(status, 0)
            self.assertEqual(data[8:16], b"NEPTUNE ")
            self.assertEqual(CSW.unpack(data[36:]), (b"USBS", 0x1234, 0, 0))

            status, actual, banner = client.transfer(USBIP_DIR_IN, 4, 256)
            self.assertEqual(status, 0)
            self.assertIn(b"virtual console", banner)
            client.transfer(USBIP_DIR_OUT, 3, 12, payload=b"fft-status\r\n")
            status, actual, console = client.transfer(USBIP_DIR_IN, 4, 256)
            self.assertEqual(status, 0)
            self.assertIn(b"N=65536", console)

            rndis_init = struct.pack("<6I", 2, 24, 7, 1, 0, 0x4000)
            self.assertEqual(
                client.control(SetupPacket(0x21, 0, 0, 0, len(rndis_init)), rndis_init)[:2],
                (0, len(rndis_init)),
            )
            status, actual, response = client.control(SetupPacket(0xA1, 1, 0, 0, 64))
            self.assertEqual(status, 0)
            self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000002)
            self.assertEqual(struct.unpack_from("<I", response, 8)[0], 7)
        finally:
            client.close()

    def test_interrupt_notifications_are_edge_triggered(self):
        client = USBIPClient(self.server.address)
        try:
            client.control(SetupPacket(0x00, 9, 1, 0, 0))

            # CDC ACM gets one initial SERIAL_STATE indication.  A resubmitted
            # interrupt URB must wait until the line state changes instead of
            # completing continuously in a tight host-driver loop.
            self.assertEqual(
                client.transfer(USBIP_DIR_IN, 5, 10)[2],
                b"\xa1\x20\0\0\x03\0\x02\0\x03\0",
            )
            cdc_pending = client.send_submit(USBIP_DIR_IN, 5, 10)
            self.assertEqual(
                client.control(SetupPacket(0x21, 0x22, 3, 3, 0))[:2],
                (0, 0),
            )
            sequence, status, actual, notification = client.receive_submit()
            self.assertEqual(sequence, cdc_pending)
            self.assertEqual((status, actual, notification), (
                0, 10, b"\xa1\x20\0\0\x03\0\x02\0\x03\0"
            ))
            self.assertEqual(
                client.unlink(client.send_submit(USBIP_DIR_IN, 5, 10)),
                -errno.ECONNRESET,
            )

            # RNDIS RESPONSE_AVAILABLE is likewise one edge per response.  It
            # must not repeat merely because GET_ENCAPSULATED_RESPONSE has not
            # consumed the response body yet.
            rndis_pending = client.send_submit(USBIP_DIR_IN, 2, 8)
            initialize = struct.pack("<6I", 2, 24, 7, 1, 0, 0x4000)
            client.control(
                SetupPacket(0x21, 0, 0, 0, len(initialize)), initialize
            )
            sequence, status, actual, notification = client.receive_submit()
            self.assertEqual(sequence, rndis_pending)
            self.assertEqual(
                (status, actual, notification),
                (0, 8, b"\xa1\x01\0\0\0\0\0\0"),
            )

            waiting = client.send_submit(USBIP_DIR_IN, 2, 8)
            response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
            self.assertEqual(struct.unpack_from("<I", response)[0], 0x80000002)
            keepalive = struct.pack("<3I", 8, 12, 9)
            client.control(
                SetupPacket(0x21, 0, 0, 0, len(keepalive)), keepalive
            )
            sequence, status, actual, notification = client.receive_submit()
            self.assertEqual(sequence, waiting)
            self.assertEqual(
                (status, actual, notification),
                (0, 8, b"\xa1\x01\0\0\0\0\0\0"),
            )
            keepalive_response = client.control(
                SetupPacket(0xA1, 1, 0, 0, 1025)
            )[2]
            self.assertEqual(
                struct.unpack_from("<I", keepalive_response)[0], 0x80000008
            )
            self.assertEqual(
                client.unlink(client.send_submit(USBIP_DIR_IN, 2, 8)),
                -errno.ECONNRESET,
            )
        finally:
            client.close()

    def test_rndis_arp_dhcp_and_icmp_contacts(self):
        client = USBIPClient(self.server.address)
        device_mac = bytes.fromhex(
            self.twin.usb.mac_addresses.device.replace(":", " ")
        )
        try:
            self.enable_rndis(client)

            query = struct.pack(
                "<7I", 4, 76, 9, 0x01010101, 48, 20, 0
            ) + b"\0" * 48
            client.control(SetupPacket(0x21, 0, 0, 0, len(query)), query)
            query_response = client.control(SetupPacket(0xA1, 1, 0, 0, 1025))[2]
            self.assertEqual(
                query_response[24:30],
                bytes.fromhex(self.twin.usb.mac_addresses.host.replace(":", " ")),
            )

            arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
            arp += HOST_MAC + HOST_IP + b"\0" * 6 + DEVICE_IP
            self.send_ethernet(
                client, b"\xff" * 6 + HOST_MAC + b"\x08\x06" + arp
            )
            reply = self.receive_ethernet(client)[0]
            self.assertEqual(reply[:12], HOST_MAC + device_mac)
            self.assertEqual(struct.unpack("!H", reply[12:14])[0], 0x0806)
            self.assertEqual(struct.unpack("!H", reply[20:22])[0], 2)
            self.assertEqual(reply[22:28], device_mac)
            self.assertEqual(reply[28:32], DEVICE_IP)

            def dhcp_request(message_type, extra=b"", xid=0x10203040):
                bootp = bytearray(240)
                bootp[:4] = b"\x01\x01\x06\x00"
                struct.pack_into("!I", bootp, 4, xid)
                struct.pack_into("!H", bootp, 10, 0x8000)
                bootp[28:34] = HOST_MAC
                bootp[236:240] = b"\x63\x82\x53\x63"
                body = bytes(bootp) + b"\x35\x01" + bytes((message_type,)) + extra + b"\xff"
                udp = bytearray(
                    struct.pack("!HHHH", 68, 67, 8 + len(body), 0) + body
                )
                struct.pack_into(
                    "!H", udp, 6,
                    transport_checksum(b"\0" * 4, b"\xff" * 4, 17, udp),
                )
                return (
                    b"\xff" * 6
                    + HOST_MAC
                    + b"\x08\x00"
                    + ipv4_packet(b"\0" * 4, b"\xff" * 4, 17, bytes(udp))
                )

            self.send_ethernet(client, dhcp_request(1))
            offer = self.receive_ethernet(client)[0]
            source, destination, protocol, udp = parse_ipv4(offer)
            self.assertEqual((source, destination, protocol), (DEVICE_IP, b"\xff" * 4, 17))
            self.assertEqual(struct.unpack("!HH", udp[:4]), (67, 68))
            self.assertEqual(
                transport_checksum(source, destination, 17, udp), 0xFFFF
            )
            self.assertEqual(udp[8 + 16:8 + 20], HOST_IP)
            self.assertIn(b"\x35\x01\x02", udp)
            self.assertIn(b"\x36\x04" + DEVICE_IP, udp)

            request_options = b"\x32\x04" + HOST_IP + b"\x36\x04" + DEVICE_IP
            self.send_ethernet(client, dhcp_request(3, request_options))
            acknowledgement = self.receive_ethernet(client)[0]
            self.assertIn(b"\x35\x01\x05", parse_ipv4(acknowledgement)[3])

            echo = bytearray(b"\x08\x00\0\0\x12\x34\0\x01wideband")
            struct.pack_into("!H", echo, 2, internet_checksum(echo))
            self.send_ethernet(
                client,
                device_mac
                + HOST_MAC
                + b"\x08\x00"
                + ipv4_packet(HOST_IP, DEVICE_IP, 1, bytes(echo)),
            )
            echo_reply = self.receive_ethernet(client)[0]
            source, destination, protocol, icmp = parse_ipv4(echo_reply)
            self.assertEqual((source, destination, protocol), (DEVICE_IP, HOST_IP, 1))
            self.assertEqual(icmp[0:2], b"\0\0")
            self.assertEqual(internet_checksum(icmp), 0)
            self.assertEqual(icmp[4:], echo[4:])

            rndis = self.server.snapshot()["rndis"]
            self.assertEqual(rndis["device_ip"], "192.168.2.1")
            self.assertEqual(rndis["host_ip"], "192.168.2.10")
            self.assertEqual(rndis["iiod_port"], 30431)
            self.assertEqual(rndis["stats"]["arp_replies"], 1)
            self.assertEqual(rndis["stats"]["dhcp_replies"], 2)
            self.assertEqual(rndis["stats"]["icmp_replies"], 1)
        finally:
            client.close()

    def test_rndis_tcp_iiod_version_print_fin_and_closed_port_reset(self):
        client = USBIPClient(self.server.address)
        device_mac = bytes.fromhex(
            self.twin.usb.mac_addresses.device.replace(":", " ")
        )
        source_port = 49152

        def send_tcp(sequence, acknowledgement, flags, payload=b"",
                     destination_port=30431, options=b""):
            segment = tcp_segment(
                HOST_IP,
                DEVICE_IP,
                source_port,
                destination_port,
                sequence,
                acknowledgement,
                flags,
                payload,
                options=options,
            )
            self.send_ethernet(
                client,
                device_mac
                + HOST_MAC
                + b"\x08\x00"
                + ipv4_packet(HOST_IP, DEVICE_IP, 6, segment),
            )

        try:
            self.enable_rndis(client)

            send_tcp(100, 0, 0x02, destination_port=12345)
            reset = parse_tcp(self.receive_ethernet(client)[0])
            self.assertEqual(reset["flags"], 0x14)
            self.assertEqual(reset["acknowledgement"], 101)

            client_sequence = 1000
            send_tcp(
                client_sequence,
                0,
                0x02,
                options=b"\x02\x04" + struct.pack("!H", 1460),
            )
            syn_ack = parse_tcp(self.receive_ethernet(client)[0])
            self.assertEqual(syn_ack["flags"] & 0x12, 0x12)
            self.assertEqual(syn_ack["acknowledgement"], client_sequence + 1)
            server_sequence = syn_ack["sequence"] + 1
            client_sequence += 1
            send_tcp(client_sequence, server_sequence, 0x10)

            version_command = b"VERSION\r\n"
            send_tcp(client_sequence, server_sequence, 0x18, version_command)
            client_sequence += len(version_command)
            version_segments = [
                parse_tcp(frame) for frame in self.receive_ethernet(client)
            ]
            self.assertTrue(
                all(item["acknowledgement"] == client_sequence for item in version_segments)
            )
            version = b"".join(item["payload"] for item in version_segments)
            self.assertEqual(version, b"0.26.v0.26  \n")
            server_sequence += len(version)
            send_tcp(client_sequence, server_sequence, 0x10)

            print_command = b"PRINT\n"
            send_tcp(client_sequence, server_sequence, 0x18, print_command)
            client_sequence += len(print_command)
            print_segments = [
                parse_tcp(frame) for frame in self.receive_ethernet(client)
            ]
            printed = b"".join(item["payload"] for item in print_segments)
            self.assertIn(b"<?xml", printed)
            self.assertIn(b"cf-ad9361-lpc", printed)
            first_line, xml_and_newline = printed.split(b"\n", 1)
            self.assertEqual(int(first_line), len(xml_and_newline) - 1)
            server_sequence += len(printed)
            send_tcp(client_sequence, server_sequence, 0x10)

            send_tcp(client_sequence, server_sequence, 0x11)
            client_sequence += 1
            closing = [parse_tcp(frame) for frame in self.receive_ethernet(client)]
            fin = [item for item in closing if item["flags"] & 0x01]
            self.assertEqual(len(fin), 1)
            self.assertEqual(fin[0]["sequence"], server_sequence)
            self.assertEqual(fin[0]["acknowledgement"], client_sequence)
            send_tcp(client_sequence, server_sequence + 1, 0x10)
            self.assertEqual(self.server.snapshot()["rndis"]["tcp_state"], "closed")
        finally:
            client.close()

    def test_rndis_tcp_uses_configured_iiod_backend(self):
        self.server.stop()
        device_mac = bytes.fromhex(
            self.twin.usb.mac_addresses.device.replace(":", " ")
        )
        source_port = 49153
        with IIODServer(self.twin.iio, port=0) as backend:
            self.server = USBIPServer(
                self.twin.usb, port=0, iiod_backend=backend.address
            ).start()
            client = USBIPClient(self.server.address)
            try:
                self.enable_rndis(client)

                def send(sequence, acknowledgement, flags, payload=b"", options=b""):
                    segment = tcp_segment(
                        HOST_IP, DEVICE_IP, source_port, 30431, sequence,
                        acknowledgement, flags, payload, options=options
                    )
                    self.send_ethernet(
                        client,
                        device_mac + HOST_MAC + b"\x08\x00"
                        + ipv4_packet(HOST_IP, DEVICE_IP, 6, segment),
                    )

                send(2000, 0, 0x02, options=b"\x02\x04\x05\xb4")
                syn_ack = parse_tcp(self.receive_ethernet(client)[0])
                client_sequence = 2001
                server_sequence = syn_ack["sequence"] + 1
                send(client_sequence, server_sequence, 0x10)
                send(client_sequence, server_sequence, 0x18, b"VERSION\r\n")
                client_sequence += 9

                received = b""
                for _ in range(3):
                    segments = [
                        parse_tcp(frame) for frame in self.receive_ethernet(client)
                    ]
                    received += b"".join(item["payload"] for item in segments)
                    if received:
                        break
                self.assertEqual(received, b"0.26.v0.26  \n")
                self.assertEqual(
                    self.server.snapshot()["iiod_backend"], list(backend.address)
                )
            finally:
                client.close()

    def test_pending_bulk_in_can_be_unlinked(self):
        client = USBIPClient(self.server.address)
        try:
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            client.control(SetupPacket(0x41, 1, 0, 5, 0))
            pending = client.send_submit(USBIP_DIR_IN, 6, 64)
            self.assertEqual(client.unlink(pending), -errno.ECONNRESET)
        finally:
            client.close()

    def test_control_header_and_setup_packet_must_agree(self):
        client = USBIPClient(self.server.address)
        try:
            # USB/IP says one OUT byte, while the setup packet has no data
            # stage.  The byte still has to be consumed so framing survives.
            status, actual, data = client.transfer(
                USBIP_DIR_OUT,
                0,
                1,
                SetupPacket(0x00, 9, 1, 0, 0).to_bytes(),
                b"x",
            )
            self.assertEqual((status, actual, data), (-errno.EPIPE, 0, b""))
            self.assertEqual(
                client.control(SetupPacket(0x00, 9, 1, 0, 0))[:2],
                (0, 0),
            )

            # Direction is duplicated in the USB/IP and USB setup headers;
            # disagreement must not execute the SET_CONFIGURATION request.
            status, actual, data = client.transfer(
                USBIP_DIR_IN,
                0,
                0,
                SetupPacket(0x00, 9, 0, 0, 0).to_bytes(),
            )
            self.assertEqual((status, actual, data), (-errno.EPIPE, 0, b""))
            self.assertTrue(self.twin.usb.configured)
        finally:
            client.close()

    def test_bulk_requires_configuration_and_zero_length_in_completes(self):
        client = USBIPClient(self.server.address)
        try:
            status, actual, data = client.transfer(USBIP_DIR_OUT, 3, 1, payload=b"x")
            self.assertEqual((status, actual, data), (-errno.EPIPE, 0, b""))
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            status, actual, data = client.transfer(USBIP_DIR_IN, 1, 0)
            self.assertEqual((status, actual, data), (0, 0, b""))
            # A zero-length URB does not make a closed native-IIO endpoint valid.
            status, actual, data = client.transfer(USBIP_DIR_IN, 6, 0)
            self.assertEqual((status, actual, data), (-errno.EPIPE, 0, b""))
        finally:
            client.close()

    def test_class_control_validation_and_mass_storage_phase_error(self):
        client = USBIPClient(self.server.address)
        try:
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            truncated_init = struct.pack("<II", 2, 8)
            self.assertEqual(
                client.control(
                    SetupPacket(0x21, 0, 0, 0, len(truncated_init)),
                    truncated_init,
                )[:2],
                (-errno.EPIPE, 0),
            )

            # An INQUIRY advertised as host-to-device is a BOT phase error.
            cdb = b"\x12\0\0\0\x24\0".ljust(16, b"\0")
            cbw = CBW.pack(b"USBC", 0xABCD, 36, 0x00, 0, 6, cdb)
            self.assertEqual(
                client.transfer(USBIP_DIR_OUT, 2, len(cbw), payload=cbw)[:2],
                (0, len(cbw)),
            )
            client.transfer(USBIP_DIR_OUT, 2, 36, payload=b"\0" * 36)
            status, actual, response = client.transfer(USBIP_DIR_IN, 3, CSW.size)
            self.assertEqual((status, actual), (0, CSW.size))
            self.assertEqual(CSW.unpack(response), (b"USBS", 0xABCD, 36, 2))
        finally:
            client.close()

    def test_backend_pipe_closes_on_import_disconnect_and_server_stop(self):
        self.server.stop()
        backend = EOFBackend()
        try:
            self.server = USBIPServer(
                self.twin.usb, port=0, iiod_backend=backend.address
            ).start()
            client = USBIPClient(self.server.address)
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            client.control(SetupPacket(0x41, 1, 0, 5, 0))
            self.assertTrue(backend.accepted.wait(2))
            client.close()
            self.assertTrue(backend.peer_closed.wait(2))

            # A second import gets a fresh backend, and stop() must detach it
            # even when the importing client remains connected.
            backend.close()
            backend = EOFBackend()
            self.server.stop()
            self.server = USBIPServer(
                self.twin.usb, port=0, iiod_backend=backend.address
            ).start()
            client = USBIPClient(self.server.address)
            client.control(SetupPacket(0x00, 9, 1, 0, 0))
            client.control(SetupPacket(0x41, 1, 0, 5, 0))
            self.assertTrue(backend.accepted.wait(2))
            self.server.stop()
            self.assertTrue(backend.peer_closed.wait(2))
            client.close()
        finally:
            backend.close()

    def test_native_iio_can_bridge_to_a_real_tcp_iiod_backend(self):
        self.server.stop()
        with IIODServer(self.twin.iio, port=0) as backend:
            self.server = USBIPServer(
                self.twin.usb,
                port=0,
                iiod_backend=backend.address,
            ).start()
            client = USBIPClient(self.server.address)
            try:
                client.control(SetupPacket(0x00, 9, 1, 0, 0))
                client.control(SetupPacket(0x41, 1, 0, 5, 0))
                client.transfer(
                    USBIP_DIR_OUT, 4, len(b"VERSION\r\n"), payload=b"VERSION\r\n"
                )
                status, actual, response = client.transfer(USBIP_DIR_IN, 6, 64)
                self.assertEqual(status, 0)
                self.assertTrue(response.startswith(b"0.26."))
                self.assertEqual(self.server.snapshot()["iiod_backend"], list(backend.address))
            finally:
                client.close()


class MassStorageImageTests(unittest.TestCase):
    def test_fat12_fixture_is_deterministic_and_read_only_labeled(self):
        first = build_read_only_volume()
        second = build_read_only_volume()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1_474_560)
        self.assertEqual(first[510:512], b"\x55\xaa")
        self.assertEqual(first[43:54], b"NEPTUNETWIN")
        root = 19 * 512
        self.assertEqual(first[root:root + 11], b"README  TXT")
        self.assertEqual(first[root + 11], 0x01)


if __name__ == "__main__":
    unittest.main()
