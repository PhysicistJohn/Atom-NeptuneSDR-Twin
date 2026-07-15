import socket
import unittest
import xml.etree.ElementTree as ET

from neptunesdr_twin.ad9361 import AD9361, GainMode
from neptunesdr_twin.iio import IIOContext, IIODServer, IIODSession


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


if __name__ == "__main__":
    unittest.main()
