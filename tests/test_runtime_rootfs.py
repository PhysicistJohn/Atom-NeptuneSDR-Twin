import gzip
import json
from pathlib import Path
import struct
import tempfile
import unittest

from neptunesdr_twin.boot_harness import BootArtifacts
from neptunesdr_twin.errors import FirmwareFormatError
from neptunesdr_twin.firmware import FDT_BEGIN_NODE, FDT_END, FDT_END_NODE, FDT_MAGIC, FDT_PROP
from neptunesdr_twin.runtime_rootfs import (
    CpioEntry,
    ELF32ARM,
    NewcArchive,
    QEMU_TCP_IIOD_SERVICE,
    QEMU_TCP_PROBE_INIT,
    QEMU_FFT_RUNTIME_INIT,
    audit_pluto_rootfs,
    build_iiod_probe_rootfs,
    build_qemu_fft_runtime_rootfs,
    build_qemu_tcp_probe_rootfs,
    build_qemu_tcp_rootfs,
    combine_runtime_artifacts,
    P210_REQUIRED_KERNEL_CONFIG,
)


def _elf(*, needed=(), interpreter=None, abi_floor=(3, 2, 0)):
    data = bytearray(1024)
    phoff = 52
    segments = []
    cursor = 256
    if interpreter is not None:
        raw_interpreter = interpreter.encode("ascii") + b"\0"
        data[cursor : cursor + len(raw_interpreter)] = raw_interpreter
        segments.append((3, cursor, cursor, cursor, len(raw_interpreter), len(raw_interpreter), 4, 1))
        cursor += len(raw_interpreter)
        cursor = (cursor + 3) & ~3

    strings = b"\0" + b"".join(item.encode("ascii") + b"\0" for item in needed)
    string_offsets = []
    offset = 1
    for item in needed:
        string_offsets.append(offset)
        offset += len(item) + 1
    string_position = 512
    data[string_position : string_position + len(strings)] = strings
    dynamic_position = 384
    dynamic = b"".join(struct.pack("<II", 1, value) for value in string_offsets)
    dynamic += struct.pack("<II", 5, string_position)
    dynamic += struct.pack("<II", 10, len(strings))
    dynamic += struct.pack("<II", 0, 0)
    data[dynamic_position : dynamic_position + len(dynamic)] = dynamic
    segments.append((2, dynamic_position, dynamic_position, dynamic_position, len(dynamic), len(dynamic), 6, 4))

    note = struct.pack("<III", 4, 16, 1) + b"GNU\0" + struct.pack("<IIII", 0, *abi_floor)
    note_position = 448
    data[note_position : note_position + len(note)] = note
    segments.append((4, note_position, note_position, note_position, len(note), len(note), 4, 4))
    # The LOAD segment maps all virtual addresses one-to-one.
    segments.insert(0, (1, 0, 0, 0, len(data), len(data), 5, 0x1000))
    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    header = struct.pack(
        "<16sHHIIIIIHHHHHH",
        ident,
        3,
        40,
        1,
        0,
        phoff,
        0,
        0x05000400,
        52,
        32,
        len(segments),
        0,
        0,
        0,
    )
    data[:52] = header
    for index, segment in enumerate(segments):
        struct.pack_into("<IIIIIIII", data, phoff + index * 32, *segment)
    return bytes(data)


def _entry(name, data=b"", mode=0o100644, inode=1):
    return CpioEntry(name, inode, mode, 0, 0, 1, 0, data)


def _synthetic_rootfs():
    iiod = _elf(needed=("libiio.so.0",), interpreter="/lib/ld-linux-armhf.so.3")
    libc = _elf()
    loader = _elf(abi_floor=(3, 2, 0))
    entries = [
        _entry(".", mode=0o040755, inode=1),
        _entry("init", b"#!/bin/sh\nexec /sbin/init\n", 0o100755, 2),
        _entry("bin/busybox", b"busybox", 0o100755, 3),
        _entry("sbin/init", b"../bin/busybox", 0o120777, 4),
        _entry("usr/sbin/iiod", iiod, 0o100755, 5),
        _entry("usr/lib/libiio.so.0", b"libiio.so.0.26", 0o120777, 6),
        _entry("usr/lib/libiio.so.0.26", b"test-libiio", 0o100755, 7),
        _entry("lib/libc.so.6", libc, 0o100755, 8),
        _entry("lib/ld-linux-armhf.so.3", loader, 0o100755, 9),
        _entry(
            "etc/device_config",
            b'PRODUCT="PlutoSDR (ADALM-PLUTO)"\nUSBPID=0xb673\nENDPOINTS=3\n',
            inode=10,
        ),
        _entry(
            "etc/init.d/S23udc",
            b'IIOD_OPTS="-D -n $ENDPOINTS -F /dev/iio_ffs"\n/usr/sbin/iiod $IIOD_OPTS\n',
            0o100755,
            11,
        ),
        _entry("opt/VERSIONS", b"device-fw v0.39\n", inode=12),
    ]
    return NewcArchive(entries).to_gzip()


def _p210_kernel_payload(version="4.14.0"):
    config = "\n".join("%s=y" % key for key in P210_REQUIRED_KERNEL_CONFIG).encode("ascii") + b"\n"
    raw = (
        b"raw-kernel\0Linux version "
        + version.encode("ascii")
        + b" (test@builder) #1 SMP\0IKCFG_ST"
        + gzip.compress(config, mtime=0)
        + b"IKCFG_ED"
    )
    return b"zImage-prefix" + gzip.compress(raw, mtime=0) + b"zImage-suffix"


def _pad4(data):
    return data + b"\0" * ((-len(data)) % 4)


def _p210_dtb(usb_mode="host", phy_address=0):
    nodes = [
        (
            "memory",
            {"device_type": b"memory\0", "reg": struct.pack(">II", 0, 0x20000000)},
            [],
        ),
        (
            "amba",
            {},
            [
                (
                    "ethernet@e000b000",
                    {
                        "compatible": b"cdns,zynq-gem\0cdns,gem\0",
                        "status": b"okay\0",
                        "phy-handle": struct.pack(">I", 8),
                        "phy-mode": b"rgmii-id\0",
                    },
                    [
                        (
                            "phy@0",
                            {"reg": struct.pack(">I", phy_address), "phandle": struct.pack(">I", 8)},
                            [],
                        )
                    ],
                ),
                (
                    "usb@e0002000",
                    {
                        "compatible": b"xlnx,zynq-usb-2.20a\0chipidea,usb2\0",
                        "status": b"okay\0",
                        "dr_mode": usb_mode.encode("ascii") + b"\0",
                    },
                    [],
                ),
            ],
        ),
    ]
    root_properties = {"compatible": b"xlnx,zynq-7000\0"}
    names = []

    def collect(properties, children):
        for key in properties:
            if key not in names:
                names.append(key)
        for _name, nested_properties, nested_children in children:
            collect(nested_properties, nested_children)

    collect(root_properties, nodes)
    strings = b""
    offsets = {}
    for name in names:
        offsets[name] = len(strings)
        strings += name.encode("ascii") + b"\0"

    def emit(name, properties, children):
        result = struct.pack(">I", FDT_BEGIN_NODE) + _pad4(name.encode("ascii") + b"\0")
        for key, value in properties.items():
            result += struct.pack(">III", FDT_PROP, len(value), offsets[key]) + _pad4(value)
        for child in children:
            result += emit(*child)
        return result + struct.pack(">I", FDT_END_NODE)

    structure = emit("", root_properties, nodes) + struct.pack(">I", FDT_END)
    reserve = b"\0" * 16
    structure_offset = 40 + len(reserve)
    strings_offset = structure_offset + len(structure)
    total = strings_offset + len(strings)
    return struct.pack(
        ">10I",
        FDT_MAGIC,
        total,
        structure_offset,
        strings_offset,
        40,
        17,
        16,
        0,
        len(strings),
        len(structure),
    ) + reserve + structure + strings


class RuntimeRootfsTests(unittest.TestCase):
    def test_newc_round_trip_and_relative_symlink_within_root(self):
        archive = NewcArchive(
            [
                _entry(".", mode=0o040755),
                _entry("bin/busybox", b"real", 0o100755, 2),
                _entry("sbin/init", b"../bin/busybox", 0o120777, 3),
            ]
        )
        parsed = NewcArchive.parse(archive.to_bytes())
        self.assertEqual(parsed.read("/sbin/init"), b"real")
        self.assertEqual(parsed.read("/sbin/init", follow_symlinks=False), b"../bin/busybox")

    def test_newc_rejects_escaping_archive_path(self):
        with self.assertRaises(FirmwareFormatError):
            NewcArchive([_entry("../escape")])

    def test_elf_proves_arm_hard_float_dynamic_contract(self):
        elf = ELF32ARM(
            _elf(
                needed=("libiio.so.0", "libc.so.6"),
                interpreter="/lib/ld-linux-armhf.so.3",
            )
        )
        self.assertEqual(elf.eabi_version, 5)
        self.assertEqual(elf.float_abi, "hard")
        self.assertEqual(elf.interpreter, "/lib/ld-linux-armhf.so.3")
        self.assertEqual(elf.needed, ("libiio.so.0", "libc.so.6"))
        self.assertEqual(elf.linux_abi_floor, (3, 2, 0))

    def test_rootfs_audit_checks_iiod_loader_library_and_service(self):
        report = audit_pluto_rootfs(_synthetic_rootfs())
        self.assertEqual(report.arm_eabi, 5)
        self.assertEqual(report.float_abi, "hard")
        self.assertEqual(report.linux_abi_floor, (3, 2, 0))
        self.assertEqual(report.libiio_version, "0.26")
        self.assertEqual(report.firmware_version, "v0.39")
        self.assertEqual(report.usb_pid, 0xB673)
        self.assertEqual(report.iiod_functionfs_endpoints, 3)
        self.assertEqual(report.service_command, "/usr/sbin/iiod -D -n 3 -F /dev/iio_ffs")

    def test_probe_images_are_explicit_and_keep_released_rootfs_unchanged(self):
        source_bytes = _synthetic_rootfs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "released.cpio.gz"
            source.write_bytes(source_bytes)
            exec_probe = build_iiod_probe_rootfs(source, root / "exec.cpio.gz")
            tcp_probe = build_qemu_tcp_rootfs(source, root / "tcp.cpio.gz")
            tcp_probe_init = build_qemu_tcp_probe_rootfs(
                source, root / "tcp-init.cpio.gz"
            )
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertIn(b"iiod -V", NewcArchive.from_gzip(exec_probe.read_bytes()).read("/init"))
            service = NewcArchive.from_gzip(tcp_probe.read_bytes()).read(
                "/etc/init.d/S24iiod-network-qemu"
            )
            self.assertEqual(service, QEMU_TCP_IIOD_SERVICE)
            self.assertIn(b"-p 30431", service)
            probe_init = NewcArchive.from_gzip(tcp_probe_init.read_bytes()).read("/init")
            self.assertEqual(probe_init, QEMU_TCP_PROBE_INIT)
            self.assertIn(b"ifconfig eth0 10.0.2.15", probe_init)
            self.assertIn(b"exec /usr/sbin/iiod -D -p 30431", probe_init)

    def test_fft_runtime_injects_only_static_arm_streamer_and_explicit_init(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.cpio.gz"
            source.write_bytes(_synthetic_rootfs())
            streamer = root / "streamer"
            streamer.write_bytes(_elf(interpreter=None, needed=()))
            output = build_qemu_fft_runtime_rootfs(
                source, streamer, root / "fft.cpio.gz"
            )
            parsed = NewcArchive.from_gzip(output.read_bytes())
            self.assertEqual(
                parsed.read("/usr/bin/neptune-fft-streamer"), streamer.read_bytes()
            )
            self.assertEqual(parsed.read("/init"), QEMU_FFT_RUNTIME_INIT)
            self.assertIn(b"cpu-online", QEMU_FFT_RUNTIME_INIT)
            self.assertIn(b"iiod -D -p 30431 &", QEMU_FFT_RUNTIME_INIT)
            self.assertIn(b"fft-streamer-exec=30432", QEMU_FFT_RUNTIME_INIT)

    def test_combined_runtime_enforces_kernel_abi_floor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = root / "kernel"
            dtb = root / "dtb"
            ramdisk = root / "ramdisk"
            kernel.write_bytes(_p210_kernel_payload())
            dtb.write_bytes(_p210_dtb())
            ramdisk.write_bytes(_synthetic_rootfs())
            p210 = BootArtifacts(root / "p210.tar", "p210-sd-boot", kernel, dtb)
            pluto = BootArtifacts(root / "pluto.zip", "pluto-release-zip", kernel, dtb, ramdisk)
            candidate = combine_runtime_artifacts(
                p210,
                pluto,
                root / "out",
                p210_kernel_name="Linux-4.14.0",
            )
            self.assertTrue(candidate.abi_compatible)
            self.assertEqual(candidate.artifacts.ramdisk.read_bytes(), ramdisk.read_bytes())
            self.assertFalse(candidate.devicetree.hardware_usb_gadget_possible)
            self.assertFalse(candidate.devicetree.qemu_gem_phy_matches)
            ethernet = candidate.devicetree.to_dict()["ethernet"]
            self.assertEqual(ethernet["stock_qemu_expected_phy_address"], 7)
            self.assertFalse(ethernet["stock_qemu_phy_matches"])
            self.assertNotIn("qemu_expected_phy_address", ethernet)
            self.assertTrue(
                any("repository's P210-enabled machine" in item for item in candidate.limitations)
            )
            self.assertTrue(any("configures its only enabled USB controller as host" in item for item in candidate.limitations))
            with self.assertRaises(FirmwareFormatError):
                combine_runtime_artifacts(
                    p210,
                    pluto,
                    root / "old",
                    p210_kernel_name="Linux-2.6.35",
                )

    def test_runtime_lock_pins_derived_real_artifact_hashes(self):
        lock_path = (
            Path(__file__).parents[1]
            / "src"
            / "neptunesdr_twin"
            / "data"
            / "runtime-lock.json"
        )
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(lock["schema"], 1)
        self.assertEqual(
            lock["inputs"]["plutosdr-fw-v0.39"]["rootfs_sha256"],
            "95eb57d76db1cd06e7ae81d8fa1c6e3567b8c792b6d0eaa4db6e8bb8a921ba77",
        )
        self.assertEqual(lock["upstream_runtime_source"]["default_tcp_port"], 30431)
        self.assertEqual(
            lock["runtime_contact_findings"]["public_p210_usb"]["dr_mode"],
            "host",
        )
        self.assertEqual(
            lock["runtime_contact_findings"]["stock_qemu_ethernet"]["public_p210_phy_address"],
            0,
        )
        self.assertIn("non-functional", lock["community_p210_recipe"]["known_status_at_commit"])


if __name__ == "__main__":
    unittest.main()
