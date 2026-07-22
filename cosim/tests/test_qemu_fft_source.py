"""Contract and QEMU-API regression checks for the P210 FFT device."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
COSIM = ROOT / "cosim"
QEMU_SOURCES = COSIM / "qemu-10.0.2"
SOURCE = QEMU_SOURCES / "hw/misc/p210_fft.c"
HEADER = QEMU_SOURCES / "include/hw/misc/p210_fft.h"
FIRMWAVE_ROOT = Path(
    os.environ.get("NEPTUNESDR_FIRMWAVE_ROOT", ROOT.parent / "Atom-NeptuneSDR_Firmwave")
)
ABI_DOC = FIRMWAVE_ROOT / "docs/P210_FFT_ABI.md"
INTERFACE_SPEC = FIRMWAVE_ROOT / "specs/p210-firmware-interface-v1.json"
FFT_BASE = 0x7C450000


def _defines() -> dict[str, int]:
    values: dict[str, int] = {}
    pattern = re.compile(
        r"^#define\s+(P210_FFT_[A-Z0-9_]+)\s+"
        r"(0x[0-9a-fA-F]+|[0-9]+)U?\b",
        re.MULTILINE,
    )
    for name, raw in pattern.findall(HEADER.read_text()):
        values[name] = int(raw, 0)
    shift_pattern = re.compile(
        r"^#define\s+(P210_FFT_[A-Z0-9_]+)\s+\(1U << ([0-9]+)\)",
        re.MULTILINE,
    )
    for name, shift in shift_pattern.findall(HEADER.read_text()):
        values[name] = 1 << int(shift)
    return values


def _qemu_build_dir() -> Path:
    override = os.environ.get("P210_QEMU_BUILD_DIR")
    if override:
        return Path(override)
    return ROOT / ".cache/qemu-p210/src/qemu-10.0.2/build-p210"


def _qemu_binary() -> Path:
    override = os.environ.get("P210_QEMU_BINARY")
    if override:
        return Path(override)
    return _qemu_build_dir() / "qemu-system-arm"


class _QTest:
    """Minimal client for QEMU's line-oriented qtest protocol."""

    def __enter__(self) -> "_QTest":
        binary = _qemu_binary()
        if not binary.is_file():
            raise unittest.SkipTest(
                "set P210_QEMU_BINARY to an integrated P210 QEMU binary"
            )
        self.process = subprocess.Popen(
            [
                str(binary),
                "-machine",
                "xilinx-zynq-a9,p210=on",
                "-display",
                "none",
                "-nodefaults",
                "-qtest",
                "stdio",
                "-S",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if self.readl(FFT_BASE) != _defines()["P210_FFT_ID"]:
            self.close()
            raise unittest.SkipTest("QEMU binary does not contain p210-fft")
        return self

    def command(self, command: str) -> str:
        process = self.process
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(command + "\n")
        process.stdin.flush()
        response = process.stdout.readline().strip()
        if not response.startswith("OK"):
            raise AssertionError("qtest command failed: %s -> %s" % (command, response))
        return response[2:].strip()

    def readl(self, address: int) -> int:
        return int(self.command("readl 0x%x" % address), 0)

    def writel(self, address: int, value: int) -> None:
        self.command("writel 0x%x 0x%x" % (address, value))

    def write(self, address: int, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        self.command("b64write 0x%x 0x%x %s" % (address, len(data), encoded))

    def read(self, address: int, size: int) -> bytes:
        encoded = self.command("b64read 0x%x 0x%x" % (address, size))
        return base64.b64decode(encoded)

    def close(self) -> None:
        process = self.process
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _compile_with_qemu_flags(source: Path, output: Path) -> None:
    database_path = _qemu_build_dir() / "compile_commands.json"
    if not database_path.is_file():
        raise unittest.SkipTest(
            "set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build"
        )
    database = json.loads(database_path.read_text())
    entry = next(
        item for item in database if item["file"].endswith("hw/misc/applesmc.c")
    )
    command = shlex.split(entry["command"])
    original_output = entry["output"]
    command = [str(source) if item == entry["file"] else item for item in command]
    command = [
        str(output)
        if item == original_output
        else str(output) + ".d"
        if item == original_output + ".d"
        else item
        for item in command
    ]
    command.insert(1, "-I" + str(QEMU_SOURCES / "include"))
    result = subprocess.run(
        command,
        cwd=entry["directory"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout)


class P210FFTSourceTests(unittest.TestCase):
    def test_public_register_abi_is_exact_and_non_overlapping(self) -> None:
        definitions = _defines()
        expected = {
            "P210_FFT_REG_ID": 0x000,
            "P210_FFT_REG_VERSION": 0x004,
            "P210_FFT_REG_CAPABILITIES": 0x008,
            "P210_FFT_REG_CONTROL": 0x00C,
            "P210_FFT_REG_STATUS": 0x010,
            "P210_FFT_REG_ERROR_CODE": 0x014,
            "P210_FFT_REG_LOG2_N": 0x018,
            "P210_FFT_REG_CHANNEL_COUNT": 0x01C,
            "P210_FFT_REG_CHANNEL_MASK": 0x020,
            "P210_FFT_REG_INPUT_ADDR": 0x024,
            "P210_FFT_REG_INPUT_BYTES": 0x028,
            "P210_FFT_REG_OUTPUT_ADDR": 0x02C,
            "P210_FFT_REG_OUTPUT_BYTES": 0x030,
            "P210_FFT_REG_SEQUENCE": 0x034,
            "P210_FFT_REG_RESULT_SEQUENCE": 0x038,
            "P210_FFT_REG_COMPLETED_LO": 0x03C,
            "P210_FFT_REG_COMPLETED_HI": 0x040,
            "P210_FFT_REG_ERROR_COUNT_LO": 0x044,
            "P210_FFT_REG_ERROR_COUNT_HI": 0x048,
            "P210_FFT_REG_BINS_WRITTEN": 0x04C,
            "P210_FFT_REG_MIN_LOG2_N": 0x050,
            "P210_FFT_REG_MAX_LOG2_N": 0x054,
        }
        self.assertEqual(
            {name: definitions[name] for name in expected}, expected
        )
        self.assertEqual(len(set(expected.values())), len(expected))
        self.assertTrue(all(offset % 4 == 0 for offset in expected.values()))
        self.assertLess(max(expected.values()), definitions["P210_FFT_MMIO_SIZE"])

    def test_identity_size_limits_and_errors_are_firmware_visible(self) -> None:
        definitions = _defines()
        self.assertEqual(struct.pack("<I", definitions["P210_FFT_ID"]), b"NFFT")
        self.assertEqual(definitions["P210_FFT_VERSION"], 0x00010000)
        self.assertEqual(definitions["P210_FFT_MIN_LOG2_N"], 10)
        self.assertEqual(definitions["P210_FFT_MAX_LOG2_N"], 16)
        self.assertEqual(1 << definitions["P210_FFT_MAX_LOG2_N"], 65_536)
        self.assertEqual(definitions["P210_FFT_MAX_CHANNELS"], 2)
        self.assertEqual(definitions["P210_FFT_ERROR_BUFFER_OVERLAP"], 10)

    def test_qemu_header_refines_the_canonical_firmwave_interface(self) -> None:
        interface = json.loads(INTERFACE_SPEC.read_text(encoding="utf-8"))
        self.assertEqual(
            interface["schema"], "neptunesdr.p210-firmware-interface/v1"
        )
        fft = interface["pl_fft_abi"]
        definitions = _defines()
        self.assertEqual(int(fft["base_address"], 16), FFT_BASE)
        self.assertEqual(int(fft["span_bytes"], 16), definitions["P210_FFT_MMIO_SIZE"])
        self.assertEqual(int(fft["identity"], 16), definitions["P210_FFT_ID"])
        self.assertEqual(int(fft["version"], 16), definitions["P210_FFT_VERSION"])
        self.assertEqual(fft["minimum_log2_n"], definitions["P210_FFT_MIN_LOG2_N"])
        self.assertEqual(fft["maximum_log2_n"], definitions["P210_FFT_MAX_LOG2_N"])
        self.assertEqual(fft["maximum_channels"], definitions["P210_FFT_MAX_CHANNELS"])

        for name, value in fft["registers"].items():
            self.assertEqual(int(value, 16), definitions["P210_FFT_REG_" + name])
        for name, value in fft["control_bits"].items():
            self.assertEqual(int(value, 16), definitions["P210_FFT_CONTROL_" + name])
        for name, value in fft["status_bits"].items():
            self.assertEqual(int(value, 16), definitions["P210_FFT_STATUS_" + name])
        for name, value in fft["capability_bits"].items():
            self.assertEqual(int(value, 16), definitions["P210_FFT_CAP_" + name])
        for name, value in fft["error_codes"].items():
            self.assertEqual(value, definitions["P210_FFT_ERROR_" + name])
        self.assertEqual(
            int(fft["capabilities_value"], 16),
            sum(int(value, 16) for value in fft["capability_bits"].values()),
        )

    def test_source_executes_integer_fft_and_dma_not_fake_completion(self) -> None:
        source = SOURCE.read_text()
        for contact in (
            "p210_fft_cordic",
            "p210_fft_bit_reverse",
            "p210_fft_radix2",
            "for (span = 2; span <= count; span <<= 1)",
            "dma_memory_read(&address_space_memory",
            "dma_memory_write(&address_space_memory",
            "product_real / (INT64_C(1) << P210_FFT_CORDIC_FRAC_BITS)",
            "(int64_t)real[i] * real[i]",
            "P210_FFT_ERROR_BUFFER_OVERLAP",
            "VMSTATE_UINT64(completed_count",
            "VMSTATE_UINT64(error_count",
        ):
            self.assertIn(contact, source)
        self.assertNotIn("#include <math.h>", source)
        self.assertIsNone(re.search(r"\b(?:sin|cos|sqrt|pow)\s*\(", source))
        self.assertNotRegex(source, r"\b(?:float|double)\s+[A-Za-z_]")

    def test_documentation_locks_memory_order_scaling_and_irq(self) -> None:
        document = ABI_DOC.read_text()
        for contact in (
            "0x7c450000",
            "GIC SPI 58",
            "signed 16-bit little-endian I",
            "time-major, channel-interleaved",
            "Output is channel-major",
            "natural FFT order `k=0..N-1`",
            "overall `1/N` amplitude scale",
            "input and output ranges overlap",
            "NSFT packet framing",
            "bitstream already contains an FFT",
        ):
            self.assertIn(contact, document)

    def test_source_compiles_with_pinned_qemu_10_flags(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p210-fft-compile-") as tmp:
            _compile_with_qemu_flags(SOURCE, Path(tmp) / "p210_fft.o")

    def test_integrated_qemu_executes_fft_and_rejects_overlap(self) -> None:
        definitions = _defines()
        count = 1024
        amplitude = 16_384
        input_address = 0x01000000
        output_address = 0x01010000
        quadrature = (
            (amplitude, 0),
            (0, amplitude),
            (-amplitude, 0),
            (0, -amplitude),
        )
        iq = b"".join(
            struct.pack("<hh", *quadrature[index & 3])
            for index in range(count)
        )

        with _QTest() as qtest:
            qtest.write(input_address, iq)
            for register, value in (
                ("P210_FFT_REG_LOG2_N", 10),
                ("P210_FFT_REG_CHANNEL_COUNT", 1),
                ("P210_FFT_REG_CHANNEL_MASK", 1),
                ("P210_FFT_REG_INPUT_ADDR", input_address),
                ("P210_FFT_REG_INPUT_BYTES", len(iq)),
                ("P210_FFT_REG_OUTPUT_ADDR", output_address),
                ("P210_FFT_REG_OUTPUT_BYTES", count * 4),
                ("P210_FFT_REG_SEQUENCE", 0x12345678),
            ):
                qtest.writel(FFT_BASE + definitions[register], value)
            qtest.writel(
                FFT_BASE + definitions["P210_FFT_REG_CONTROL"],
                definitions["P210_FFT_CONTROL_START"]
                | definitions["P210_FFT_CONTROL_IRQ_ENABLE"],
            )

            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_STATUS"]),
                definitions["P210_FFT_STATUS_DONE"]
                | definitions["P210_FFT_STATUS_IRQ_PENDING"],
            )
            self.assertEqual(
                qtest.readl(
                    FFT_BASE + definitions["P210_FFT_REG_RESULT_SEQUENCE"]
                ),
                0x12345678,
            )
            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_COMPLETED_LO"]),
                1,
            )
            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_BINS_WRITTEN"]),
                count,
            )
            powers = struct.unpack("<1024I", qtest.read(output_address, count * 4))
            self.assertEqual(powers[count // 4], amplitude * amplitude)
            self.assertEqual(sum(powers), amplitude * amplitude)

            qtest.writel(
                FFT_BASE + definitions["P210_FFT_REG_STATUS"],
                definitions["P210_FFT_STATUS_DONE"],
            )
            qtest.writel(
                FFT_BASE + definitions["P210_FFT_REG_OUTPUT_ADDR"],
                input_address,
            )
            qtest.writel(
                FFT_BASE + definitions["P210_FFT_REG_CONTROL"],
                definitions["P210_FFT_CONTROL_START"],
            )
            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_STATUS"]),
                definitions["P210_FFT_STATUS_ERROR"],
            )
            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_ERROR_CODE"]),
                definitions["P210_FFT_ERROR_BUFFER_OVERLAP"],
            )
            self.assertEqual(
                qtest.readl(
                    FFT_BASE + definitions["P210_FFT_REG_ERROR_COUNT_LO"]
                ),
                1,
            )

    def test_integrated_qemu_executes_65536_bins_for_two_channels(self) -> None:
        definitions = _defines()
        count = 65_536
        channel_0_amplitude = 16_000
        channel_1_amplitude = 8_000
        input_address = 0x01000000
        output_address = 0x01200000
        channel_0 = (
            (channel_0_amplitude, 0),
            (0, channel_0_amplitude),
            (-channel_0_amplitude, 0),
            (0, -channel_0_amplitude),
        )
        channel_1 = (
            (channel_1_amplitude, 0),
            (0, -channel_1_amplitude),
            (-channel_1_amplitude, 0),
            (0, channel_1_amplitude),
        )
        iq = b"".join(
            struct.pack(
                "<hhhh", *channel_0[index & 3], *channel_1[index & 3]
            )
            for index in range(count)
        )

        with _QTest() as qtest:
            qtest.write(input_address, iq)
            for register, value in (
                ("P210_FFT_REG_LOG2_N", 16),
                ("P210_FFT_REG_CHANNEL_COUNT", 2),
                ("P210_FFT_REG_CHANNEL_MASK", 3),
                ("P210_FFT_REG_INPUT_ADDR", input_address),
                ("P210_FFT_REG_INPUT_BYTES", len(iq)),
                ("P210_FFT_REG_OUTPUT_ADDR", output_address),
                ("P210_FFT_REG_OUTPUT_BYTES", count * 2 * 4),
                ("P210_FFT_REG_SEQUENCE", 0xFEEDBEEF),
            ):
                qtest.writel(FFT_BASE + definitions[register], value)
            qtest.writel(
                FFT_BASE + definitions["P210_FFT_REG_CONTROL"],
                definitions["P210_FFT_CONTROL_START"],
            )

            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_STATUS"]),
                definitions["P210_FFT_STATUS_DONE"],
            )
            self.assertEqual(
                qtest.readl(FFT_BASE + definitions["P210_FFT_REG_BINS_WRITTEN"]),
                count * 2,
            )
            self.assertEqual(
                qtest.readl(output_address + (count // 4) * 4),
                channel_0_amplitude * channel_0_amplitude,
            )
            self.assertEqual(qtest.readl(output_address), 0)
            channel_1_output = output_address + count * 4
            self.assertEqual(
                qtest.readl(channel_1_output + (3 * count // 4) * 4),
                channel_1_amplitude * channel_1_amplitude,
            )
            self.assertEqual(qtest.readl(channel_1_output), 0)


if __name__ == "__main__":
    unittest.main()
