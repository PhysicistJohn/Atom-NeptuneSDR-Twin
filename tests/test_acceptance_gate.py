"""The readiness gates must never turn skips or stale evidence into a PASS."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from typing import Optional
import unittest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "acceptance_gate.py"


def _run(*arguments: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, str(GATE), *arguments),
        cwd=str(cwd or ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


class AcceptanceGateTests(unittest.TestCase):
    def test_skip_count_and_exact_reason_are_part_of_source_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-skips-") as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_sample.py").write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class Sample(unittest.TestCase):
                        def test_pass(self):
                            self.assertTrue(True)

                        @unittest.skip("QEMU intentionally absent")
                        def test_live(self):
                            pass
                    """
                ),
                encoding="utf-8",
            )
            summary = root / "summary.json"
            accepted = _run(
                "test-suite",
                "--start-dir",
                str(tests),
                "--label",
                "fixture",
                "--summary",
                str(summary),
                "--expect-skips",
                "1",
                "--expect-skip-reason",
                "1:QEMU intentionally absent",
                "--min-tests",
                "2",
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout)
            payload = json.loads(summary.read_text(encoding="utf-8"))
            self.assertTrue(payload["gate_pass"])
            self.assertEqual(payload["results"]["skipped"], 1)

            rejected = _run(
                "test-suite",
                "--start-dir",
                str(tests),
                "--label",
                "fixture",
                "--summary",
                str(summary),
                "--expect-skips",
                "1",
                "--expect-skip-reason",
                "1:different reason",
                "--min-tests",
                "2",
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertFalse(json.loads(summary.read_text())["gate_pass"])

    def test_required_live_test_cannot_be_deleted_silently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-required-") as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_sample.py").write_text(
                "import unittest\nclass Sample(unittest.TestCase):\n"
                "    def test_present(self): pass\n",
                encoding="utf-8",
            )
            result = _run(
                "test-suite",
                "--start-dir",
                str(tests),
                "--label",
                "fixture",
                "--summary",
                str(root / "summary.json"),
                "--expect-skips",
                "0",
                "--min-tests",
                "1",
                "--require-test",
                "test_sample.Sample.test_missing",
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("TEST_GATE MISSING", result.stdout)

    def test_full_manifest_binds_source_qemu_tests_and_firmware_hashes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-manifest-") as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            (root / ".gitignore").write_text(".evidence/\n.qemu-build/\n")
            (root / "source.txt").write_text("tested source\n", encoding="utf-8")
            subprocess.run(("git", "add", ".gitignore", "source.txt"), cwd=root, check=True)
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Gate Test",
                    "-c",
                    "user.email=gate@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ),
                cwd=root,
                check=True,
            )
            output = root / ".evidence" / "run-1"
            started = _run(
                "start",
                "--root",
                str(root),
                "--output",
                str(output),
                "--run-id",
                "run-1",
                "--mode",
                "full",
                cwd=root,
            )
            self.assertEqual(started.returncode, 0, started.stdout)

            summary = {
                "schema": "atom-neptune-acceptance-v1",
                "gate_pass": True,
                "results": {"tests_run": 1, "skipped": 0},
            }
            qemu_build = root / ".qemu-build"
            qemu_build.mkdir()
            qemu = qemu_build / "qemu-system-arm"
            qemu.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'QEMU emulator version 10.0.2'\n",
                encoding="utf-8",
            )
            qemu.chmod(0o755)
            (qemu_build / "compile_commands.json").write_text("[]\n")
            qemu_identity = {
                "available": True,
                "sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
                "compile_commands": {
                    "sha256": hashlib.sha256(
                        (qemu_build / "compile_commands.json").read_bytes()
                    ).hexdigest()
                },
            }
            (output / "reference.json").write_text(json.dumps(summary), encoding="utf-8")
            (output / "cosim.json").write_text(
                json.dumps({**summary, "qemu_environment": qemu_identity}),
                encoding="utf-8",
            )
            firmware_log = output / "firmware-runtime.log"
            firmware_log.write_text("P210_RUNTIME PASS\n", encoding="utf-8")
            runtime = output / "runtime"
            runtime.mkdir()
            required = (
                "p210-kernel.bin",
                "p210-devicetree.dtb",
                "qemu-fft-runtime.cpio.gz",
                "p210-qemu.log",
                "p210-qemu-iio-info.txt",
                "p210-qemu-fft.nsft",
                "p210-qemu-fft-report.json",
            )
            (runtime / "runtime-manifest.json").write_text(
                json.dumps(
                    {
                        "abi_compatible": True,
                        "execution_target": {
                            "machine": "xilinx-zynq-a9,p210=on",
                            "cpu_count": 2,
                            "memory_bytes": 536_870_912,
                        },
                    }
                )
            )
            for name in required:
                (runtime / name).write_bytes((name + "\n").encode())
            iio_markers = (
                "iio_info version: 0.26 (git tag:a0eca0d)",
                "Backend version: 0.26 (git tag: v0.26)",
                "IIO context has 5 devices:",
                "iio:device0: ad9361-phy",
                "cf-ad9361-lpc (buffer capable)",
                "rf_bandwidth value: 50000000",
                "sampling_frequency value: 61440000",
            )
            (runtime / "p210-qemu-iio-info.txt").write_text("\n".join(iio_markers))
            serial_markers = (
                "AD936x Rev 2 successfully initialized",
                "NEPTUNE_RUNTIME cpu-online=0-1",
                "NEPTUNE_FFT accelerator-id=5446464e version=00010000 caps=0000003f",
                "NEPTUNE_FFT rf-bandwidth=50000000 sample-rate=61440000",
                "NEPTUNE_FFT ready port=30432 n=65536 channels=2 input=iio-dmac-cpu-copy",
                "macb e000b000.ethernet eth0: link up",
                "NEPTUNE_FFT transmitted sequence=1 bins=131072 bytes=262288",
            )
            (runtime / "p210-qemu.log").write_text("\n".join(serial_markers))
            report = {
                "status": "passed",
                "transport": "guest-arm-nsft-v1-tcp",
                "sequence": 1,
                "timestamp_ns": 100,
                "config_epoch": 0,
                "sample_rate_hz": 61_440_000,
                "center_frequency_hz": 2_400_000_000,
                "wire_bytes_received": 262_288,
                "socket_bytes_received": 262_288,
                "crc_checked": True,
                "tone_contract_checked": True,
                "channels": [
                    {
                        "channel": channel,
                        "fft_size": 65_536,
                        "bins": 65_536,
                        "encoding": "UINT16_LOG_POWER",
                        "sample_rate_hz": 61_440_000,
                        "center_frequency_hz": 2_400_000_000,
                        "sequence": 1,
                        "timestamp_ns": 100,
                        "config_epoch": 0,
                        "peak_bin": peak,
                        "peak_dbfs": power,
                    }
                    for channel, peak, power in ((0, 5_120, -2.53), (1, 13_312, -6.08))
                ],
            }
            (runtime / "p210-qemu-fft-report.json").write_text(json.dumps(report))
            (runtime / "p210-qemu-fft.nsft").write_bytes(b"x" * 262_288)

            # A PASS marker alone must not bless a semantically false capture.
            invalid = dict(report)
            invalid["channels"] = [dict(item) for item in report["channels"]]
            invalid["channels"][0]["fft_size"] = 1024
            (runtime / "p210-qemu-fft-report.json").write_text(json.dumps(invalid))
            rejected = _run(
                "finish-full",
                "--root", str(root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())
            (runtime / "p210-qemu-fft-report.json").write_text(json.dumps(report))

            finished = _run(
                "finish-full",
                "--root",
                str(root),
                "--output",
                str(output),
                "--reference-summary",
                str(output / "reference.json"),
                "--cosim-summary",
                str(output / "cosim.json"),
                "--qemu",
                str(qemu),
                "--qemu-build-dir",
                str(qemu_build),
                "--firmware-log",
                str(firmware_log),
                "--runtime-dir",
                str(runtime),
                cwd=root,
            )
            self.assertEqual(finished.returncode, 0, finished.stdout)
            manifest = json.loads(
                (output / "acceptance-manifest.json").read_text(encoding="utf-8")
            )
            commit = subprocess.check_output(
                ("git", "rev-parse", "HEAD"), cwd=root, text=True
            ).strip()
            self.assertEqual(manifest["source"]["commit"], commit)
            self.assertEqual(manifest["tests"]["total_skipped"], 0)
            self.assertTrue(manifest["firmware"]["pass"])
            self.assertEqual(
                manifest["firmware"]["verified_claims"]["peak_bins"],
                [5_120, 13_312],
            )
            self.assertEqual(
                manifest["qemu"]["sha256"],
                hashlib.sha256(qemu.read_bytes()).hexdigest(),
            )
            names = {item["path"] for item in manifest["firmware"]["runtime_artifacts"]}
            self.assertIn("runtime/p210-qemu-fft.nsft", names)

    def test_manifest_rejects_source_changed_after_run_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-stale-") as directory:
            root = Path(directory)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            (root / ".gitignore").write_text(".evidence/\n")
            source = root / "source.txt"
            source.write_text("before\n")
            subprocess.run(("git", "add", ".gitignore", "source.txt"), cwd=root, check=True)
            subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Gate Test",
                    "-c",
                    "user.email=gate@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ),
                cwd=root,
                check=True,
            )
            output = root / ".evidence"
            result = _run(
                "start",
                "--root",
                str(root),
                "--output",
                str(output),
                "--run-id",
                "stale",
                "--mode",
                "full",
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            source.write_text("after\n")
            # Validation stops at the source fingerprint, before it needs the
            # deliberately absent QEMU/firmware fixture arguments.
            result = _run(
                "finish-full",
                "--root",
                str(root),
                "--output",
                str(output),
                "--reference-summary",
                str(output / "missing-reference.json"),
                "--cosim-summary",
                str(output / "missing-cosim.json"),
                "--qemu",
                str(root / "missing-qemu"),
                "--qemu-build-dir",
                str(root / "missing-build"),
                "--firmware-log",
                str(output / "missing-runtime.log"),
                "--runtime-dir",
                str(output / "missing-runtime"),
                cwd=root,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source state changed", result.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
