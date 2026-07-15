import hashlib
import unittest

from neptunesdr_twin.errors import USBProtocolError
from neptunesdr_twin.usb import (
    IIO_REQ_CLOSE_PIPE,
    IIO_REQ_OPEN_PIPE,
    IIO_REQ_RESET_PIPES,
    SetupPacket,
    USBControlEndpoint,
    USBPersonality,
    decode_string_descriptor,
    derive_deterministic_macs,
    load_observed_usb_profile,
    parse_configuration_descriptor,
    parse_descriptor_stream,
    validate_configuration_descriptor,
)


class ObservedDescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_observed_usb_profile()

    def test_observed_device_descriptor_is_byte_locked(self):
        device = self.profile.parsed_device
        self.assertEqual(len(device.raw), 18)
        self.assertEqual(device.vendor_id, 0x0456)
        self.assertEqual(device.product_id, 0xB673)
        self.assertEqual(device.device_version, 0x0515)
        self.assertEqual(device.max_packet_size_ep0, 64)
        self.assertEqual(
            hashlib.sha256(device.raw).hexdigest(),
            "8aa4a8888e46bdc26ace6cd2b7b52e446bd9756b1724313f215f9ecd3beb6e7b",
        )

    def test_observed_configuration_is_byte_locked_and_structural(self):
        configuration = self.profile.parsed_configuration
        self.assertEqual(configuration.total_length, 0x00DA)
        self.assertEqual(configuration.declared_interface_count, 6)
        self.assertEqual(configuration.attributes, 0x80)
        self.assertEqual(configuration.max_power_ma, 500)
        self.assertEqual(
            hashlib.sha256(configuration.raw).hexdigest(),
            "b0ce03f9df66e25608b0810224277e93dbbe30a031faf33f7d803151a3ad10a6",
        )
        self.assertEqual(
            configuration.endpoint_addresses,
            (
                0x82,
                0x81,
                0x01,
                0x83,
                0x02,
                0x85,
                0x84,
                0x03,
                0x86,
                0x04,
                0x87,
                0x05,
                0x88,
                0x06,
            ),
        )
        self.assertEqual(configuration.interface(2).endpoints[1].interval, 1)
        self.assertEqual(configuration.interface(5).string_index, 15)
        self.assertEqual(
            configuration.interface(5).endpoint_addresses,
            (0x86, 0x04, 0x87, 0x05, 0x88, 0x06),
        )

    def test_parser_rejects_bad_bounds_and_logical_counts(self):
        with self.assertRaises(USBProtocolError):
            parse_configuration_descriptor(self.profile.configuration_descriptor[:-1])
        with self.assertRaises(USBProtocolError):
            parse_descriptor_stream(b"\x00\x04")

        bad = bytearray(self.profile.configuration_descriptor)
        bad[4] = 5
        with self.assertRaises(USBProtocolError):
            validate_configuration_descriptor(bytes(bad))

    def test_string_descriptors_include_linux_blank_allocations(self):
        table = self.profile.string_table(serial="p210-test-serial")
        self.assertEqual(table.descriptor(0), b"\x04\x03\x09\x04")
        self.assertEqual(decode_string_descriptor(table.descriptor(3)), "p210-test-serial")
        self.assertEqual(table.descriptor(8), b"\x02\x03")
        self.assertEqual(decode_string_descriptor(table.descriptor(15)), "IIO")

    def test_normal_and_dfu_personality_metadata(self):
        normal = self.profile.personality(USBPersonality.NORMAL)
        dfu = self.profile.personality("dfu")
        self.assertEqual((normal.vendor_id, normal.product_id), (0x0456, 0xB673))
        self.assertEqual((dfu.vendor_id, dfu.product_id), (0x0456, 0xB674))
        self.assertIn("firmware.dfu", dfu.alternate_settings)


class ControlEndpointTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_observed_usb_profile()
        self.ep0 = USBControlEndpoint(self.profile, serial="my-p210")

    def test_get_descriptors_honours_host_length(self):
        get_device = SetupPacket(0x80, 6, 0x0100, 0, 8)
        self.assertEqual(
            self.ep0.control_transfer(get_device), self.profile.device_descriptor[:8]
        )
        get_configuration = SetupPacket(0x80, 6, 0x0200, 0, 9)
        self.assertEqual(
            self.ep0.handle_setup(get_configuration),
            self.profile.configuration_descriptor[:9],
        )
        get_serial = SetupPacket(0x80, 6, 0x0303, 0x0409, 255)
        self.assertEqual(
            decode_string_descriptor(self.ep0.control_transfer(get_serial)), "my-p210"
        )

    def test_setup_packet_round_trip(self):
        packet = SetupPacket(0x80, 6, 0x0200, 0, 218)
        self.assertEqual(SetupPacket.from_bytes(packet.to_bytes()), packet)

    def test_configuration_and_native_iio_pipe_requests(self):
        with self.assertRaises(USBProtocolError):
            self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_OPEN_PIPE, 0, 5, 0))

        self.ep0.control_transfer(SetupPacket(0x00, 9, 1, 0, 0))
        self.assertTrue(self.ep0.configured)
        self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_OPEN_PIPE, 0, 5, 0))
        self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_OPEN_PIPE, 2, 5, 0))
        self.assertEqual(self.ep0.iio_pipe_states, (True, False, True))
        self.assertEqual(self.ep0.snapshot()["iio_open_pipes"], [0, 2])
        self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_CLOSE_PIPE, 0, 5, 0))
        self.assertEqual(self.ep0.iio_pipe_states, (False, False, True))
        self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_RESET_PIPES, 0, 5, 0))
        self.assertEqual(self.ep0.iio_pipe_states, (False, False, False))

        with self.assertRaises(USBProtocolError):
            self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_OPEN_PIPE, 3, 5, 0))

    def test_set_configuration_zero_resets_pipe_state(self):
        self.ep0.control_transfer(SetupPacket(0x00, 9, 1, 0, 0))
        self.ep0.control_transfer(SetupPacket(0x41, IIO_REQ_OPEN_PIPE, 1, 5, 0))
        self.ep0.control_transfer(SetupPacket(0x00, 9, 0, 0, 0))
        self.assertFalse(self.ep0.configured)
        self.assertEqual(self.ep0.open_pipes, set())

    def test_deterministic_mac_addresses_match_firmware_script(self):
        addresses = derive_deterministic_macs(
            "104473222a87000618000600473ed57ae0"
        )
        self.assertEqual(addresses.host, "00:e0:22:9b:ee:20")
        self.assertEqual(addresses.device, "00:05:f7:d9:dd:2f")
        self.assertEqual(
            derive_deterministic_macs("serial\n"),
            derive_deterministic_macs("serial"),
        )


if __name__ == "__main__":
    unittest.main()
