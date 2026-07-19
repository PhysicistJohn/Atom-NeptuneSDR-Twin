"""The two real trust boundaries fail closed: network NSFT bytes in, fetched firmware in."""

import hashlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neptunesdr_twin.errors import FirmwareFormatError
from neptunesdr_twin.fft import (
    PACKET_HEADER_BYTES,
    PacketCRCError,
    PayloadEncoding,
    SpectrumPacket,
)
from neptunesdr_twin.firmware import fetch_locked_artifact, load_firmware_lock
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


class FirmwareTrustTests(unittest.TestCase):
    def test_firmware_lock_has_content_addresses(self):
        lock = load_firmware_lock()
        self.assertIn("p210-sd-boot", lock["artifacts"])
        self.assertIn("p210-system-xsa", lock["artifacts"])
        for artifact in lock["artifacts"].values():
            self.assertEqual(len(artifact["sha256"]), 64)

    def test_locked_fetch_rejects_wrong_bytes(self):
        payload = b"locked bytes"
        lock = {
            "artifacts": {
                "item": {
                    "url": "https://invalid.example/item",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            destination = root / "item.bin"
            with mock.patch(
                "neptunesdr_twin.firmware.urllib.request.urlopen",
                return_value=io.BytesIO(payload),
            ):
                self.assertEqual(
                    fetch_locked_artifact("item", destination, lock_path),
                    destination,
                )
            self.assertEqual(destination.read_bytes(), payload)

            with mock.patch(
                "neptunesdr_twin.firmware.urllib.request.urlopen",
                return_value=io.BytesIO(payload + b"!"),
            ):
                with self.assertRaises(FirmwareFormatError):
                    fetch_locked_artifact("item", destination, lock_path)


if __name__ == "__main__":
    unittest.main()
