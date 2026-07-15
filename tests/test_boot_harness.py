import io
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
import zlib

from neptunesdr_twin.boot_harness import (
    BootArtifacts,
    build_qemu_command,
    extract_boot_artifacts,
    extract_fit_image,
    extract_p210_bundle,
    locate_qemu_system_arm,
    locked_artifact_path,
    run_qemu_boot,
    verify_locked_artifact,
)
from neptunesdr_twin.errors import FirmwareFormatError
from neptunesdr_twin.firmware import (
    FDT_BEGIN_NODE,
    FDT_END,
    FDT_END_NODE,
    FDT_MAGIC,
    FDT_PROP,
    UIMAGE_MAGIC,
    FlattenedDeviceTree,
)


def _pad4(value):
    return value + b"\0" * ((-len(value)) % 4)


def _make_fdt(root_properties=None, children=None):
    root_properties = root_properties or {}
    children = children or []
    names = []
    for key in root_properties:
        if key not in names:
            names.append(key)

    def collect(nodes):
        for _node_name, properties, nested in nodes:
            for key in properties:
                if key not in names:
                    names.append(key)
            collect(nested)

    collect(children)
    strings = b""
    offsets = {}
    for name in names:
        offsets[name] = len(strings)
        strings += name.encode("ascii") + b"\0"

    def emit_node(name, properties, nested):
        result = struct.pack(">I", FDT_BEGIN_NODE) + _pad4(name.encode("utf-8") + b"\0")
        for property_name, value in properties.items():
            result += struct.pack(">III", FDT_PROP, len(value), offsets[property_name])
            result += _pad4(value)
        for child in nested:
            result += emit_node(*child)
        return result + struct.pack(">I", FDT_END_NODE)

    structure = emit_node("", root_properties, children) + struct.pack(">I", FDT_END)
    reserve = b"\0" * 16
    reserve_offset = 40
    structure_offset = reserve_offset + len(reserve)
    strings_offset = structure_offset + len(structure)
    total = strings_offset + len(strings)
    header = struct.pack(
        ">10I",
        FDT_MAGIC,
        total,
        structure_offset,
        strings_offset,
        reserve_offset,
        17,
        16,
        0,
        len(strings),
        len(structure),
    )
    return header + reserve + structure + strings


def _string(value):
    return value.encode("utf-8") + b"\0"


def _make_target_dtb(label="target"):
    return _make_fdt(
        {"compatible": _string("xlnx,zynq-7000")},
        [
            (
                "chosen",
                {"bootargs": _string("console=ttyPS0,115200 test=%s" % label)},
                [],
            )
        ],
    )


def _make_p210_dtb():
    compatible_nodes = [
        ("dmac@0", {"compatible": _string("adi,axi-dmac-1.00.a")}, []),
        ("dmac@1", {"compatible": _string("adi,axi-dmac-1.00.a")}, []),
        ("axi-ad9361", {"compatible": _string("adi,axi-ad9361-6.00.a")}, []),
        (
            "axi-ad9361-dds",
            {"compatible": _string("adi,axi-ad9361-dds-6.00.a")},
            [],
        ),
        ("ethernet", {"compatible": _string("cdns,zynq-gem")}, []),
        ("usb", {"compatible": _string("chipidea,usb2")}, []),
        (
            "ad9361",
            {
                "compatible": _string("adi,ad9361"),
                "adi,2rx-2tx-mode-enable": b"",
            },
            [],
        ),
    ]
    return _make_fdt(
        {"compatible": _string("xlnx,zynq-7000")},
        [
            ("memory", {"reg": struct.pack(">II", 0, 0x20000000)}, []),
            (
                "chosen",
                {"bootargs": _string("console=ttyPS0,115200 root=/dev/mmcblk0p2")},
                [],
            ),
        ]
        + compatible_nodes,
    )


def _make_fit():
    dtb_a = _make_target_dtb("a")
    dtb_b = _make_target_dtb("b")
    return _make_fdt(
        {
            "description": _string("test FIT"),
            "magic": _string("ITB PlutoSDR (ADALM-PLUTO)"),
        },
        [
            (
                "images",
                {},
                [
                    (
                        "kernel@1",
                        {
                            "data": b"kernel-one",
                            "type": _string("kernel"),
                            "compression": _string("none"),
                        },
                        [],
                    ),
                    (
                        "ramdisk@1",
                        {
                            "data": b"\x1f\x8btest-ramdisk",
                            "type": _string("ramdisk"),
                            "compression": _string("gzip"),
                        },
                        [],
                    ),
                    (
                        "fdt@1",
                        {
                            "data": dtb_a,
                            "type": _string("flat_dt"),
                            "compression": _string("none"),
                        },
                        [],
                    ),
                    (
                        "fdt@2",
                        {
                            "data": dtb_b,
                            "type": _string("flat_dt"),
                            "compression": _string("none"),
                        },
                        [],
                    ),
                ],
            ),
            (
                "configurations",
                {"default": _string("config@0")},
                [
                    (
                        "config@0",
                        {
                            "kernel": _string("kernel@1"),
                            "ramdisk": _string("ramdisk@1"),
                            "fdt": _string("fdt@1"),
                        },
                        [],
                    ),
                    (
                        "config@1",
                        {
                            "kernel": _string("kernel@1"),
                            "ramdisk": _string("ramdisk@1"),
                            "fdt": _string("fdt@2"),
                        },
                        [],
                    ),
                ],
            ),
        ],
    )


def _make_uimage(payload=b"p210-kernel"):
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
        b"Linux-test".ljust(32, b"\0"),
    )
    header_crc = zlib.crc32(header) & 0xFFFFFFFF
    return header[:4] + struct.pack(">I", header_crc) + header[8:] + payload


def _write_p210_tar(path):
    members = {
        "./uImage": _make_uimage(),
        "./devicetree.dtb": _make_p210_dtb(),
        "./uEnv.txt": b"bootargs=console=ttyPS0,115200 root=/dev/mmcblk0p2\n",
        "./BOOT.BIN": b"B" * 1_000_001,
    }
    with tarfile.open(path, "w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _with_dfu_suffix(data):
    suffix_without_crc = struct.pack(
        "<HHHH3sB", 0xFFFF, 0xB673, 0x0456, 0x0100, b"UFD", 16
    )
    partial = data + suffix_without_crc
    return partial + struct.pack("<I", (~zlib.crc32(partial)) & 0xFFFFFFFF)


class BootHarnessTests(unittest.TestCase):
    def test_fit_extracts_default_and_selected_inline_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = extract_fit_image(_make_fit(), root / "default", source_name="test.frm")
            selected = extract_fit_image(
                _make_fit(), root / "selected", "config@1", source_name="test.frm"
            )
            self.assertEqual(default.kernel.read_bytes(), b"kernel-one")
            self.assertEqual(default.ramdisk.read_bytes(), b"\x1f\x8btest-ramdisk")
            self.assertEqual(default.configuration, "config@0")
            self.assertEqual(selected.configuration, "config@1")
            self.assertIn("test=a", default.bootargs)
            self.assertIn("test=b", selected.bootargs)
            self.assertEqual(
                FlattenedDeviceTree(selected.devicetree.read_bytes()).root.string("compatible"),
                "xlnx,zynq-7000",
            )

    def test_fit_rejects_wrong_inline_image_type(self):
        fit = bytearray(_make_fit())
        tree = FlattenedDeviceTree(bytes(fit))
        payload = tree.find("/images/kernel@1").properties["data"]
        index = bytes(fit).find(payload)
        # Corrupting the type preserves the FDT layout but violates the contact.
        type_value = tree.find("/images/kernel@1").properties["type"]
        type_index = bytes(fit).find(type_value, index + len(payload))
        fit[type_index : type_index + len(type_value)] = _string("ramdisk")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FirmwareFormatError):
                extract_fit_image(bytes(fit), temporary)

    def test_p210_tar_extracts_crc_checked_uimage_payload_and_dtb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "p210.tar.zst"
            _write_p210_tar(archive_path)
            artifacts = extract_p210_bundle(archive_path, root / "out")
            self.assertEqual(artifacts.kernel.read_bytes(), b"p210-kernel")
            self.assertIn("root=/dev/mmcblk0p2", artifacts.bootargs)
            self.assertEqual(artifacts.kind, "p210-sd-boot")
            self.assertEqual(artifacts.execution_scope, "kernel-entry-only")
            self.assertTrue(artifacts.non_emulated_components)

    def test_pluto_zip_detection_uses_pluto_frm_not_other_frm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "release.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("pluto.frm", _make_fit())
                archive.writestr("boot.frm", b"not a FIT")
            artifacts = extract_boot_artifacts(source, root / "out")
            self.assertEqual(artifacts.kind, "pluto-release-zip")
            self.assertEqual(artifacts.kernel.read_bytes(), b"kernel-one")

    def test_command_is_argv_only_and_disables_monitor_and_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = root / "kernel with spaces"
            dtb = root / "target.dtb"
            ramdisk = root / "ramdisk.img"
            kernel.write_bytes(b"kernel")
            dtb.write_bytes(_make_target_dtb())
            ramdisk.write_bytes(b"ramdisk")
            artifacts = BootArtifacts(root, "test", kernel, dtb, ramdisk, "console=test")
            command = build_qemu_command(artifacts, "/usr/bin/qemu-system-arm")
            self.assertEqual(command[0], "/usr/bin/qemu-system-arm")
            self.assertIn(str(kernel.resolve()), command)
            self.assertEqual(command[command.index("-nic") + 1], "none")
            self.assertEqual(command[command.index("-monitor") + 1], "none")
            self.assertNotIn("-drive", command)
            self.assertNotIn("-usbdevice", command)
            with self.assertRaises(ValueError):
                build_qemu_command(artifacts, memory_mib=2049)

    def test_qemu_locator_requires_an_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "qemu-system-arm"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            self.assertEqual(locate_qemu_system_arm(executable), executable.resolve())
            with self.assertRaises(FileNotFoundError):
                locate_qemu_system_arm(Path(temporary) / "missing")

    def test_locked_artifact_path_and_verification_are_content_addressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b"locked firmware"
            digest = hashlib.sha256(data).hexdigest()
            lock = root / "lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "artifacts": {
                            "test": {
                                "url": "https://example.invalid/releases/firmware.bin",
                                "sha256": digest,
                                "bytes": len(data),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            path = locked_artifact_path("test", root / "cache", lock)
            self.assertEqual(path.parent.name, digest)
            self.assertEqual(path.name, "firmware.bin")
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            self.assertEqual(verify_locked_artifact("test", path, lock), digest)
            path.write_bytes(data + b"damaged")
            with self.assertRaises(FirmwareFormatError):
                verify_locked_artifact("test", path, lock)

    def test_bounded_runner_captures_patterns_without_a_shell(self):
        code = "print('Booting Linux'); print('Linux version 6.test')"
        result = run_qemu_boot(
            [sys.executable, "-c", code],
            timeout=2,
            patterns=(r"Booting Linux", r"Linux version"),
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)

    def test_matching_text_does_not_mask_an_early_process_failure(self):
        result = run_qemu_boot(
            [sys.executable, "-c", "print('Linux version fake'); raise SystemExit(3)"],
            timeout=2,
            patterns=(r"Linux version",),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.returncode, 3)

    def test_fatal_kernel_log_rejects_an_otherwise_matching_run(self):
        result = run_qemu_boot(
            [
                sys.executable,
                "-c",
                "print('Booting Linux'); print('Linux version test'); print('Kernel panic')",
            ],
            timeout=2,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.matched_rejections, (r"Kernel panic",))

    def test_bounded_runner_terminates_on_timeout(self):
        code = "import time; print('started', flush=True); time.sleep(10)"
        result = run_qemu_boot(
            [sys.executable, "-c", code], timeout=0.1, patterns=(r"started",)
        )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.passed)
        self.assertIn("started", result.output)

    def test_bounded_runner_caps_captured_output(self):
        result = run_qemu_boot(
            [sys.executable, "-c", "print('x' * 4096)"],
            timeout=2,
            patterns=(),
            max_output_bytes=1024,
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.output_truncated)
        self.assertEqual(len(result.output.encode("utf-8")), 1024)

    def test_cli_firmware_validation_and_qemu_default_to_dry_run(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            p210 = root / "p210.tar.zst"
            pluto = root / "pluto.zip"
            _write_p210_tar(p210)
            fit = _make_fit()
            with zipfile.ZipFile(pluto, "w") as archive:
                archive.writestr("pluto.frm", fit)
                archive.writestr("pluto.dfu", _with_dfu_suffix(fit))

            validation = subprocess.run(
                [
                    sys.executable,
                    str(repository / "scripts" / "test_firmware.py"),
                    "--p210",
                    str(p210),
                    "--pluto",
                    str(pluto),
                    "--json",
                ],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertTrue(json.loads(validation.stdout)["compatible"])

            dry_run = subprocess.run(
                [
                    sys.executable,
                    str(repository / "scripts" / "qemu_boot.py"),
                    str(p210),
                    "--work-dir",
                    str(root / "boot"),
                    "--json",
                ],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            dry_report = json.loads(dry_run.stdout)
            self.assertEqual(dry_report["mode"], "dry-run")
            self.assertEqual(dry_report["execution_scope"], "kernel-entry-only")

    def test_fetch_cli_uses_content_addressed_lock_path(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            source.write_bytes(b"download test")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            lock = root / "lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "artifacts": {
                            "test": {
                                "url": source.resolve().as_uri(),
                                "sha256": digest,
                                "bytes": source.stat().st_size,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            fetched = subprocess.run(
                [
                    sys.executable,
                    str(repository / "scripts" / "fetch_firmware.py"),
                    "test",
                    "--lock",
                    str(lock),
                    "--cache-dir",
                    str(root / "cache"),
                    "--json",
                ],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            result = json.loads(fetched.stdout)[0]
            self.assertEqual(Path(result["path"]).parent.name, digest)


if __name__ == "__main__":
    unittest.main()
