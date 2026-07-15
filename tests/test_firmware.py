import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from neptunesdr_twin.errors import FirmwareFormatError
from neptunesdr_twin.firmware import (
    DFUSuffix,
    FDT_BEGIN_NODE,
    FDT_END,
    FDT_END_NODE,
    FDT_MAGIC,
    FDT_PROP,
    FlattenedDeviceTree,
    UIMAGE_MAGIC,
    UImage,
    fetch_locked_artifact,
    load_firmware_lock,
)


def _pad4(value):
    return value + b"\0" * ((-len(value)) % 4)


def make_fdt():
    strings = b"compatible\0reg\0"
    compatible = b"vendor,board\0soc,test\0"
    structure = b""
    structure += struct.pack(">I", FDT_BEGIN_NODE) + _pad4(b"\0")
    structure += struct.pack(">III", FDT_PROP, len(compatible), 0) + _pad4(compatible)
    structure += struct.pack(">I", FDT_BEGIN_NODE) + _pad4(b"memory\0")
    structure += struct.pack(">III", FDT_PROP, 8, len(b"compatible\0"))
    structure += struct.pack(">II", 0, 0x20000000)
    structure += struct.pack(">I", FDT_END_NODE)
    structure += struct.pack(">I", FDT_END_NODE)
    structure += struct.pack(">I", FDT_END)
    reserve = b"\0" * 16
    off_reserve = 40
    off_struct = off_reserve + len(reserve)
    off_strings = off_struct + len(structure)
    total = off_strings + len(strings)
    header = struct.pack(
        ">10I", FDT_MAGIC, total, off_struct, off_strings, off_reserve, 17, 16, 0, len(strings), len(structure)
    )
    return header + reserve + structure + strings


def make_uimage(payload=b"kernel"):
    name = b"Linux-test".ljust(32, b"\0")
    header = struct.pack(
        ">7I4B32s",
        UIMAGE_MAGIC,
        0,
        1,
        len(payload),
        0x8000,
        0x8000,
        zlib.crc32(payload) & 0xFFFFFFFF,
        5,
        2,
        2,
        0,
        name,
    )
    crc = zlib.crc32(header) & 0xFFFFFFFF
    return header[:4] + struct.pack(">I", crc) + header[8:] + payload


class FirmwareParserTests(unittest.TestCase):
    def test_fdt_structure_strings_and_cells(self):
        tree = FlattenedDeviceTree(make_fdt())
        self.assertEqual(tree.root.string_list("compatible"), ("vendor,board", "soc,test"))
        self.assertEqual(tree.find("/memory").u32s("reg"), (0, 0x20000000))

    def test_fdt_rejects_truncation(self):
        data = make_fdt()
        with self.assertRaises(FirmwareFormatError):
            FlattenedDeviceTree(data[:-3])

    def test_uimage_checks_both_crcs(self):
        image = UImage(make_uimage())
        self.assertEqual(image.header.name, "Linux-test")
        self.assertEqual(image.payload, b"kernel")
        damaged = bytearray(make_uimage())
        damaged[-1] ^= 1
        with self.assertRaises(FirmwareFormatError):
            UImage(bytes(damaged))

    def test_firmware_lock_has_content_addresses(self):
        lock = load_firmware_lock()
        self.assertIn("p210-sd-boot", lock["artifacts"])
        self.assertIn("p210-system-xsa", lock["artifacts"])

    def test_locked_fetch_enforces_size_before_digest(self):
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
        for artifact in lock["artifacts"].values():
            self.assertEqual(len(artifact["sha256"]), 64)

    def test_dfu_suffix_crc_and_identity(self):
        body = b"firmware"
        suffix_without_crc = struct.pack(
            "<HHHH3sB", 0xFFFF, 0xB673, 0x0456, 0x0100, b"UFD", 16
        )
        partial = body + suffix_without_crc
        crc = (~zlib.crc32(partial)) & 0xFFFFFFFF
        artifact = partial + struct.pack("<I", crc)
        suffix = DFUSuffix.parse(artifact)
        self.assertEqual((suffix.vendor_id, suffix.product_id), (0x0456, 0xB673))
        damaged = bytearray(artifact)
        damaged[0] ^= 1
        with self.assertRaises(FirmwareFormatError):
            DFUSuffix.parse(bytes(damaged))


if __name__ == "__main__":
    unittest.main()
