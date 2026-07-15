import socket
import errno
import unittest
import xml.etree.ElementTree as ET

from neptunesdr_twin.ad9361 import AD9361, GainMode
from neptunesdr_twin.iio import (
    IIOContext,
    IIODServer,
    IIODSession,
    MAX_IIOD_PAYLOAD_BYTES,
)


class IIOContextTests(unittest.TestCase):
    def setUp(self):
        self.radio = AD9361()
        self.context = IIOContext(
            self.radio,
            rx_provider=lambda length: bytes((index & 0xFF for index in range(length))),
            tx_consumer=lambda data: None,
        )
        self.session = IIODSession(self.context)

    def test_xml_has_2x2_phy_and_four_channel_streams(self):
        xml = self.context.xml()
        root = ET.fromstring(xml)
        devices = {node.attrib["name"]: node for node in root.findall("device")}
        self.assertIn("ad9361-phy", devices)
        self.assertEqual(len(devices["cf-ad9361-lpc"].findall("channel")), 4)
        self.assertIn('format="le:S12/16&gt;&gt;0"', xml)

    def test_attribute_write_reaches_radio(self):
        payload = b"manual"
        response = self.session.execute(
            b"WRITE iio:device0 INPUT voltage0 gain_control_mode 6\r\n", payload
        )
        self.assertEqual(response, b"6\n")
        self.assertEqual(self.radio.rx_channels[0].gain_mode, GainMode.MANUAL)
        response = self.session.execute(b"READ iio:device0 INPUT voltage0 gain_control_mode\r\n")
        self.assertEqual(response, b"6\nmanual\n")

    def test_buffer_open_mask_and_data_contract(self):
        self.assertEqual(self.session.execute(b"OPEN iio:device3 128 0000000f\r\n"), b"0\n")
        result = self.session.execute(b"READBUF iio:device3 16\r\n")
        self.assertEqual(result[:3], b"16\n")
        self.assertEqual(result[3:12], b"0000000f\n")
        self.assertEqual(result[12:], bytes(range(16)))
        self.assertEqual(self.session.execute(b"CLOSE iio:device3\r\n"), b"0\n")

    def test_open_rejects_bad_counts_masks_and_cyclic_spelling(self):
        invalid = (
            b"OPEN iio:device3 0 0000000f\r\n",
            b"OPEN iio:device3 -1 0000000f\r\n",
            b"OPEN iio:device3 4294967296 0000000f\r\n",
            b"OPEN iio:device3 8 f\r\n",
            b"OPEN iio:device3 8 00000000\r\n",
            b"OPEN iio:device3 8 00000010\r\n",
            b"OPEN iio:device3 8 0000000f CIRCULAR\r\n",
            b"OPEN iio:device3 8 0000000f CYCLIC\r\n",
        )
        for command in invalid:
            with self.subTest(command=command):
                self.assertEqual(
                    self.session.execute(command),
                    ("-%d\n" % errno.EINVAL).encode("ascii"),
                )
        self.assertEqual(
            self.session.execute(b"OPEN iio:device2 8 0000000f CYCLIC\r\n"),
            b"0\n",
        )

    def test_payload_lengths_and_provider_short_reads_fail_closed(self):
        self.assertEqual(
            self.session.execute(
                b"WRITE iio:device0 INPUT voltage0 gain_control_mode 7\r\n",
                b"manual",
            ),
            ("-%d\n" % errno.EIO).encode("ascii"),
        )
        self.assertEqual(
            self.session.execute(
                (
                    "WRITE iio:device0 INPUT voltage0 gain_control_mode %d\r\n"
                    % (MAX_IIOD_PAYLOAD_BYTES + 1)
                ).encode("ascii")
            ),
            ("-%d\n" % errno.E2BIG).encode("ascii"),
        )

        short_context = IIOContext(self.radio, rx_provider=lambda length: b"x")
        short_session = IIODSession(short_context)
        self.assertEqual(
            short_session.execute(b"OPEN iio:device3 8 0000000f\r\n"), b"0\n"
        )
        self.assertEqual(
            short_session.execute(b"READBUF iio:device3 8\r\n"),
            ("-%d\n" % errno.EIO).encode("ascii"),
        )


class IIODNetworkTests(unittest.TestCase):
    def test_real_tcp_framing_print_read_and_writebuf_handshake(self):
        transmitted = []
        radio = AD9361()
        context = IIOContext(
            radio,
            rx_provider=lambda length: b"R" * length,
            tx_consumer=transmitted.append,
        )
        with IIODServer(context, port=0) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                stream = connection.makefile("rwb", buffering=0)
                stream.write(b"VERSION\r\n")
                self.assertTrue(stream.readline().startswith(b"0.26."))
                stream.write(b"PRINT\r\n")
                length = int(stream.readline())
                xml = stream.read(length)
                self.assertIn(b"cf-ad9361-lpc", xml)
                self.assertEqual(stream.read(1), b"\n")
                stream.write(b"OPEN iio:device2 8 0000000f\r\n")
                self.assertEqual(stream.readline(), b"0\n")
                stream.write(b"WRITEBUF iio:device2 8\r\n")
                self.assertEqual(stream.readline(), b"8\n")
                stream.write(b"12345678")
                self.assertEqual(stream.readline(), b"8\n")
        self.assertEqual(transmitted, [b"12345678"])

    def test_tcp_handler_rejects_oversized_and_short_payloads(self):
        radio = AD9361()
        context = IIOContext(radio, tx_consumer=lambda data: None)
        with IIODServer(context, port=0) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                stream = connection.makefile("rwb", buffering=0)
                stream.write(b"OPEN iio:device2 8 0000000f\r\n")
                self.assertEqual(stream.readline(), b"0\n")
                stream.write(
                    (
                        "WRITEBUF iio:device2 %d\r\n"
                        % (MAX_IIOD_PAYLOAD_BYTES + 1)
                    ).encode("ascii")
                )
                self.assertEqual(
                    stream.readline(),
                    ("-%d\n" % errno.E2BIG).encode("ascii"),
                )

            with socket.create_connection(server.address, timeout=2) as connection:
                stream = connection.makefile("rwb", buffering=0)
                stream.write(b"OPEN iio:device2 8 0000000f\r\n")
                self.assertEqual(stream.readline(), b"0\n")
                stream.write(b"WRITEBUF iio:device2 8\r\n")
                self.assertEqual(stream.readline(), b"8\n")
                stream.write(b"abc")
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(
                    stream.readline(),
                    ("-%d\n" % errno.EIO).encode("ascii"),
                )


if __name__ == "__main__":
    unittest.main()
