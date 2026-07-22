"""Untrusted NSFT network bytes fail closed at the Twin boundary."""

import struct
import unittest

from neptunesdr_twin.fft import (
    PACKET_HEADER_BYTES,
    PacketCRCError,
    PayloadEncoding,
    SpectrumPacket,
)
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder, SpectrumStreamError


def packet():
    return SpectrumPacket(
        sequence=1,
        channel=0,
        fft_size=256,
        sample_rate_hz=61_440_000,
        center_frequency_hz=2_400_000_000,
        timestamp_ns=123,
        config_epoch=0,
        bin_start=0,
        values_dbfs=(-80.0, -20.0, 0.0),
        encoding=PayloadEncoding.UINT16_LOG_POWER,
    )


class SpectrumStreamTrustTests(unittest.TestCase):
    def test_bad_magic_fails_closed(self):
        raw = bytearray(packet().pack())
        raw[0] ^= 0xFF
        with self.assertRaises(SpectrumStreamError):
            SpectrumStreamDecoder().feed(raw)

    def test_oversize_length_fails_closed(self):
        raw = bytearray(packet().pack())
        raw[PACKET_HEADER_BYTES - 4 : PACKET_HEADER_BYTES] = struct.pack(">I", 999_999)
        with self.assertRaises(SpectrumStreamError):
            SpectrumStreamDecoder(maximum_packet_bytes=1000).feed(raw)

    def test_corrupt_crc_fails_closed(self):
        raw = bytearray(packet().pack())
        raw[-1] ^= 0x01
        with self.assertRaises(PacketCRCError):
            SpectrumStreamDecoder().feed(raw)

if __name__ == "__main__":
    unittest.main()
