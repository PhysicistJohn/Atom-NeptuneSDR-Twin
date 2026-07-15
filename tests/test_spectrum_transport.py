import socket
import struct
import time
import unittest

from neptunesdr_twin.fft import PACKET_HEADER_BYTES, PayloadEncoding, SpectrumPacket
from neptunesdr_twin.spectrum_transport import (
    SpectrumStreamDecoder,
    SpectrumStreamError,
    SpectrumTCPPublisher,
)


def packet(sequence=1, channel=0):
    return SpectrumPacket(
        sequence=sequence,
        channel=channel,
        fft_size=256,
        sample_rate_hz=61_440_000,
        center_frequency_hz=2_400_000_000,
        timestamp_ns=123,
        config_epoch=0,
        bin_start=0,
        values_dbfs=(-80.0, -20.0, 0.0),
        encoding=PayloadEncoding.UINT16_LOG_POWER,
    )


class SpectrumStreamDecoderTests(unittest.TestCase):
    def test_fragmented_and_coalesced_packets_decode(self):
        first = packet(1).pack()
        second = packet(2, 1).pack()
        decoder = SpectrumStreamDecoder()
        self.assertEqual(decoder.feed(first[:11]), ())
        decoded = decoder.feed(first[11:] + second)
        self.assertEqual([item.sequence for item in decoded], [1, 2])
        self.assertEqual(decoder.buffered_bytes, 0)

    def test_bad_magic_and_oversize_length_fail_closed(self):
        raw = bytearray(packet().pack())
        raw[0] ^= 0xFF
        with self.assertRaises(SpectrumStreamError):
            SpectrumStreamDecoder().feed(raw)

        raw = bytearray(packet().pack())
        raw[PACKET_HEADER_BYTES - 4 : PACKET_HEADER_BYTES] = struct.pack(">I", 999_999)
        with self.assertRaises(SpectrumStreamError):
            SpectrumStreamDecoder(maximum_packet_bytes=1000).feed(raw)


class SpectrumTCPPublisherTests(unittest.TestCase):
    def test_real_tcp_stream_carries_checked_packet(self):
        publisher = SpectrumTCPPublisher().start()
        client = socket.create_connection(publisher.address, timeout=1.0)
        client.settimeout(1.0)
        try:
            deadline = time.monotonic() + 1.0
            while publisher.client_count != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(publisher.client_count, 1)
            self.assertEqual(publisher.publish((packet(9),)), 1)
            decoder = SpectrumStreamDecoder()
            decoded = ()
            while not decoded:
                decoded = decoder.feed(client.recv(4096))
            self.assertEqual(decoded[0].sequence, 9)
            self.assertEqual(publisher.counters.published_packets, 1)
        finally:
            client.close()
            publisher.stop()


if __name__ == "__main__":
    unittest.main()
