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


def _init_repository(root: Path, *, ignored: str = "") -> None:
    root.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    (root / ".gitignore").write_text(ignored, encoding="utf-8")
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


class AcceptanceGateTests(unittest.TestCase):
    def test_hidden_index_flags_cannot_hide_source_changes(self) -> None:
        cases = (
            ("--skip-worktree", "S"),
            ("--assume-unchanged", "h"),
        )
        for option, expected_tag in cases:
            with self.subTest(option=option), tempfile.TemporaryDirectory(
                prefix="gate-hidden-index-"
            ) as directory:
                root = Path(directory) / "repo"
                firmwave_root = Path(directory) / "firmwave"
                _init_repository(root, ignored=".evidence/\n")
                _init_repository(firmwave_root)

                baseline_output = root / ".evidence" / "baseline"
                baseline = _run(
                    "start",
                    "--root", str(root),
                    "--firmwave-root", str(firmwave_root),
                    "--output", str(baseline_output),
                    "--run-id", "baseline",
                    "--mode", "source",
                    cwd=root,
                )
                self.assertEqual(baseline.returncode, 0, baseline.stdout)
                baseline_state = json.loads(
                    (baseline_output / "run-state.json").read_text(encoding="utf-8")
                )["sources"]["twin"]
                self.assertTrue(baseline_state["clean"])

                subprocess.run(
                    ("git", "update-index", option, "source.txt"),
                    cwd=root,
                    check=True,
                )
                (root / "source.txt").write_text(
                    "bytes hidden from ordinary Git status\n", encoding="utf-8"
                )
                porcelain = subprocess.check_output(
                    ("git", "status", "--porcelain", "--untracked-files=no"),
                    cwd=root,
                    text=True,
                )
                self.assertEqual(porcelain, "")

                flagged_output = root / ".evidence" / "flagged"
                flagged = _run(
                    "start",
                    "--root", str(root),
                    "--firmwave-root", str(firmwave_root),
                    "--output", str(flagged_output),
                    "--run-id", "flagged",
                    "--mode", "source",
                    cwd=root,
                )
                self.assertEqual(flagged.returncode, 0, flagged.stdout)
                flagged_state = json.loads(
                    (flagged_output / "run-state.json").read_text(encoding="utf-8")
                )["sources"]["twin"]
                self.assertFalse(flagged_state["clean"])
                self.assertEqual(
                    flagged_state["hidden_index_flags"],
                    [{"path": "source.txt", "tag": expected_tag}],
                )
                self.assertNotEqual(
                    flagged_state["state_sha256"], baseline_state["state_sha256"]
                )

                full = _run(
                    "start",
                    "--root", str(root),
                    "--firmwave-root", str(firmwave_root),
                    "--output", str(root / ".evidence" / "full"),
                    "--run-id", "full",
                    "--mode", "full",
                    cwd=root,
                )
                self.assertNotEqual(full.returncode, 0, full.stdout)
                self.assertIn("hidden Git index flags", full.stdout)

    def test_full_acceptance_requires_clean_twin_and_firmwave(self) -> None:
        for dirty_repository, expected in (
            ("twin", "Twin repository must be clean for full acceptance"),
            ("firmwave", "Firmwave repository must be clean for full acceptance"),
        ):
            with self.subTest(repository=dirty_repository), tempfile.TemporaryDirectory(
                prefix="gate-clean-source-"
            ) as directory:
                root = Path(directory) / "repo"
                firmwave_root = Path(directory) / "firmwave"
                _init_repository(root)
                _init_repository(firmwave_root)
                dirty_root = root if dirty_repository == "twin" else firmwave_root
                (dirty_root / "source.txt").write_text("uncommitted source\n")

                result = _run(
                    "start",
                    "--root", str(root),
                    "--firmwave-root", str(firmwave_root),
                    "--output", str(Path(directory) / "evidence"),
                    "--run-id", "dirty",
                    "--mode", "full",
                    cwd=root,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertFalse((Path(directory) / "evidence").exists())

    def test_no_build_cache_is_bound_to_sources_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-build-cache-") as directory:
            temporary = Path(directory)
            root = temporary / "repo"
            firmwave_root = temporary / "firmwave"
            _init_repository(root)
            _init_repository(firmwave_root)
            qemu = temporary / "qemu-system-arm"
            guest = temporary / "neptune-fft-streamer"
            iio_info = temporary / "iio_info"
            iio_readdev = temporary / "iio_readdev"
            libiio = temporary / "libiio.dylib"
            qemu.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            guest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            iio_info.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            iio_readdev.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            libiio.write_bytes(b"pinned host libiio\n")
            for executable in (qemu, guest, iio_info, iio_readdev):
                executable.chmod(0o755)
            binding = temporary / "p210-runtime-build-identity.json"
            common = (
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--qemu", str(qemu),
                "--guest", str(guest),
                "--iio-info", str(iio_info),
                "--iio-readdev", str(iio_readdev),
                "--libiio", str(libiio),
                "--output", str(binding),
            )

            written = _run("write-build-cache", *common)
            self.assertEqual(written.returncode, 0, written.stdout)
            payload = json.loads(binding.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "atom-neptune-build-cache-v1")
            self.assertEqual(set(payload["sources"]), {"twin", "firmwave"})
            self.assertEqual(
                set(payload["artifacts"]),
                {
                    "qemu",
                    "guest_fft_streamer",
                    "host_iio_info",
                    "host_iio_readdev",
                    "host_libiio",
                },
            )

            verified = _run("verify-build-cache", *common)
            self.assertEqual(verified.returncode, 0, verified.stdout)

            guest.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            rejected_artifact = _run("verify-build-cache", *common)
            self.assertNotEqual(
                rejected_artifact.returncode, 0, rejected_artifact.stdout
            )
            self.assertIn("guest_fft_streamer artifact", rejected_artifact.stdout)
            guest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            libiio.write_bytes(b"tampered host libiio\n")
            rejected_libiio = _run("verify-build-cache", *common)
            self.assertNotEqual(
                rejected_libiio.returncode, 0, rejected_libiio.stdout
            )
            self.assertIn("host_libiio artifact", rejected_libiio.stdout)
            libiio.write_bytes(b"pinned host libiio\n")

            (root / "source.txt").write_text("changed Twin source\n", encoding="utf-8")
            rejected_twin = _run("verify-build-cache", *common)
            self.assertNotEqual(rejected_twin.returncode, 0, rejected_twin.stdout)
            self.assertIn("twin source", rejected_twin.stdout)
            (root / "source.txt").write_text("tested source\n", encoding="utf-8")

            (firmwave_root / "source.txt").write_text(
                "changed Firmwave source\n", encoding="utf-8"
            )
            rejected_firmwave = _run("verify-build-cache", *common)
            self.assertNotEqual(
                rejected_firmwave.returncode, 0, rejected_firmwave.stdout
            )
            self.assertIn("firmwave source", rejected_firmwave.stdout)

            launcher = (ROOT / "scripts" / "run_p210_firmware.sh").read_text(
                encoding="utf-8"
            )
            self.assertIn("write-build-cache", launcher)
            self.assertIn("verify-build-cache", launcher)
            full_gate = (ROOT / "scripts" / "accept_virtual_twin.sh").read_text(
                encoding="utf-8"
            )
            cache_verification = full_gate.index("verify-build-cache")
            self.assertLess(
                cache_verification,
                full_gate.index('build_p210_qemu.sh" --verify'),
            )
            self.assertLess(
                cache_verification, full_gate.index('"$QEMU_BINARY" --version')
            )

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
            firmwave_root = Path(directory) / "firmwave"
            _init_repository(root, ignored=".evidence/\n.qemu-build/\n")
            _init_repository(firmwave_root)
            output = root / ".evidence" / "run-1"
            started = _run(
                "start",
                "--root",
                str(root),
                "--firmwave-root",
                str(firmwave_root),
                "--output",
                str(output),
                "--run-id",
                "run-1",
                "--mode",
                "full",
                cwd=root,
            )
            self.assertEqual(started.returncode, 0, started.stdout)
            run_state = json.loads(
                (output / "run-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                run_state["sources"]["firmwave"]["repository"],
                "Atom-NeptuneSDR_Firmwave",
            )
            self.assertEqual(run_state["source"], run_state["sources"]["twin"])
            firmwave_identity = {
                key: run_state["sources"]["firmwave"][key]
                for key in ("repository", "commit", "state_sha256")
            }

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
            (output / "firmwave.json").write_text(
                json.dumps(summary), encoding="utf-8"
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
                        "firmwave_source": firmwave_identity,
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
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--firmwave-summary", str(output / "firmwave.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())
            (runtime / "p210-qemu-fft-report.json").write_text(json.dumps(report))

            # Firmwave's own test evidence is mandatory and independently
            # fail-closed; a failed gate or any skip cannot be hidden behind
            # otherwise valid Twin/QEMU/firmware evidence.
            (output / "firmwave.json").write_text(
                json.dumps({**summary, "gate_pass": False}), encoding="utf-8"
            )
            rejected = _run(
                "finish-full",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--firmwave-summary", str(output / "firmwave.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("firmwave test summary is not a passing gate", rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())

            skipped_firmwave = {
                **summary,
                "results": {"tests_run": 1, "skipped": 1},
            }
            (output / "firmwave.json").write_text(
                json.dumps(skipped_firmwave), encoding="utf-8"
            )
            rejected = _run(
                "finish-full",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--firmwave-summary", str(output / "firmwave.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("Firmwave suite contains skipped tests", rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())
            (output / "firmwave.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )

            runtime_manifest_path = runtime / "runtime-manifest.json"
            runtime_manifest = json.loads(runtime_manifest_path.read_text())
            del runtime_manifest["firmwave_source"]
            runtime_manifest_path.write_text(json.dumps(runtime_manifest))
            rejected = _run(
                "finish-full",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--firmwave-summary", str(output / "firmwave.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("no Firmwave source identity", rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())

            # Firmware evidence from a different Firmwave revision/state must
            # not be accepted even if its runtime behavior happens to pass.
            runtime_manifest["firmwave_source"] = {
                **firmwave_identity,
                "commit": "0" * 40,
            }
            runtime_manifest_path.write_text(json.dumps(runtime_manifest))
            rejected = _run(
                "finish-full",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "reference.json"),
                "--cosim-summary", str(output / "cosim.json"),
                "--firmwave-summary", str(output / "firmwave.json"),
                "--qemu", str(qemu),
                "--qemu-build-dir", str(qemu_build),
                "--firmware-log", str(firmware_log),
                "--runtime-dir", str(runtime),
                cwd=root,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
            self.assertIn("Firmwave commit", rejected.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())
            runtime_manifest["firmwave_source"] = firmwave_identity
            runtime_manifest_path.write_text(json.dumps(runtime_manifest))

            finished = _run(
                "finish-full",
                "--root",
                str(root),
                "--firmwave-root",
                str(firmwave_root),
                "--output",
                str(output),
                "--reference-summary",
                str(output / "reference.json"),
                "--cosim-summary",
                str(output / "cosim.json"),
                "--firmwave-summary",
                str(output / "firmwave.json"),
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
            self.assertEqual(manifest["sources"]["twin"], manifest["source"])
            self.assertEqual(
                manifest["sources"]["firmwave"]["commit"],
                firmwave_identity["commit"],
            )
            self.assertEqual(
                manifest["firmware"]["firmwave_source"]["state_sha256"],
                firmwave_identity["state_sha256"],
            )
            self.assertEqual(manifest["tests"]["total_skipped"], 0)
            self.assertEqual(manifest["tests"]["firmwave"], summary)
            self.assertEqual(manifest["tests"]["total_run"], 3)
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
            root = Path(directory) / "repo"
            firmwave_root = Path(directory) / "firmwave"
            _init_repository(root, ignored=".evidence/\n")
            _init_repository(firmwave_root)
            source = root / "source.txt"
            output = root / ".evidence"
            result = _run(
                "start",
                "--root",
                str(root),
                "--firmwave-root",
                str(firmwave_root),
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
                "--firmwave-root",
                str(firmwave_root),
                "--output",
                str(output),
                "--reference-summary",
                str(output / "missing-reference.json"),
                "--cosim-summary",
                str(output / "missing-cosim.json"),
                "--firmwave-summary",
                str(output / "missing-firmwave.json"),
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

    def test_manifest_rejects_firmwave_changed_after_run_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-firmwave-stale-") as directory:
            root = Path(directory) / "repo"
            firmwave_root = Path(directory) / "firmwave"
            _init_repository(root, ignored=".evidence/\n")
            _init_repository(firmwave_root)
            output = root / ".evidence"
            result = _run(
                "start",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--run-id", "firmwave-stale",
                "--mode", "full",
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            (firmwave_root / "source.txt").write_text("changed Firmwave\n")
            result = _run(
                "finish-full",
                "--root", str(root),
                "--firmwave-root", str(firmwave_root),
                "--output", str(output),
                "--reference-summary", str(output / "missing-reference.json"),
                "--cosim-summary", str(output / "missing-cosim.json"),
                "--firmwave-summary", str(output / "missing-firmwave.json"),
                "--qemu", str(root / "missing-qemu"),
                "--qemu-build-dir", str(root / "missing-build"),
                "--firmware-log", str(output / "missing-runtime.log"),
                "--runtime-dir", str(output / "missing-runtime"),
                cwd=root,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Firmwave repository source state changed", result.stdout)
            self.assertFalse((output / "acceptance-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
