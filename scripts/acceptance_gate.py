#!/usr/bin/env python3
"""Fail-closed test and evidence primitives for the virtual-twin gates.

The ordinary source gate has one explicit skip budget for tests that require
the integrated QEMU build.  Full acceptance has no skip budget.  This helper
keeps those policies out of human-readable log parsing and writes the evidence
manifest only after the tested source state is proven unchanged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO
import unittest


SCHEMA = "atom-neptune-acceptance-v1"
BUILD_CACHE_SCHEMA = "atom-neptune-build-cache-v1"
PASS_MARKER = "P210_RUNTIME PASS"
_BUILD_SKIP = "set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build"
_BINARY_SKIP = "set P210_QEMU_BINARY to an integrated P210 QEMU binary"
SOURCE_WITHOUT_QEMU_SKIPS = {
    "test_qemu_device_sources.QEMUDeviceSourceTests.test_sources_compile_with_qemu_10_flags": _BUILD_SKIP,
    "test_qemu_fft_source.P210FFTSourceTests.test_source_compiles_with_pinned_qemu_10_flags": _BUILD_SKIP,
    "test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_65536_bins_for_two_channels": _BINARY_SKIP,
    "test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_fft_and_rejects_overlap": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_cpu1_reset_control_survives_migration_and_system_reset": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_dma_decode_error_uses_standard_completion_and_qemu_log": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_irq_pending_is_masked_view_and_source_is_raw": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_pause_suspends_and_resume_completes": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_paused_inflight_queue_and_deadline_survive_migration": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_submit_while_disabled_is_ignored": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_system_reset_cancels_timer_queue_and_irq_state": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_three_channel_scan_is_contiguous_across_copy_chunks": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_tx_cyclic_suppresses_ids_done_and_interrupts": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_unsupported_two_dimensional_fields_do_not_expand_transfer": _BINARY_SKIP,
    "test_qemu_sdr_live.P210SDRLiveTests.test_xsa_capabilities_alignment_and_pause_readback": _BINARY_SKIP,
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(str(temporary), str(path))


def _run_bytes(arguments: Sequence[str], root: Path) -> bytes:
    return subprocess.check_output(arguments, cwd=str(root), stderr=subprocess.STDOUT)


def _source_state(root: Path) -> Dict[str, Any]:
    root = root.resolve()
    commit = _run_bytes(("git", "rev-parse", "HEAD"), root).decode().strip()
    branch = _run_bytes(("git", "rev-parse", "--abbrev-ref", "HEAD"), root).decode().strip()
    diff = _run_bytes(
        ("git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"), root
    )
    untracked_raw = _run_bytes(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"), root
    )
    index_raw = _run_bytes(("git", "ls-files", "-v", "-z", "--"), root)
    hidden_index_flags: List[Dict[str, str]] = []
    for encoded in index_raw.split(b"\0"):
        if not encoded:
            continue
        if len(encoded) < 3 or encoded[1:2] != b" ":
            raise ValueError("git ls-files returned a malformed index entry")
        tag = chr(encoded[0])
        # `git diff` and `git status` deliberately trust these index flags.
        # Treat sparse/skip-worktree (`S`) and assume-unchanged (lowercase)
        # entries as provenance violations so they cannot hide modified bytes.
        if tag == "S" or tag.islower():
            hidden_index_flags.append(
                {"path": os.fsdecode(encoded[2:]), "tag": tag}
            )
    untracked: List[Dict[str, Any]] = []
    for encoded in untracked_raw.split(b"\0"):
        if not encoded:
            continue
        relative = os.fsdecode(encoded)
        candidate = root / relative
        if candidate.is_file():
            untracked.append(
                {
                    "path": relative,
                    "bytes": candidate.stat().st_size,
                    "sha256": _sha256_file(candidate),
                }
            )
        else:
            untracked.append({"path": relative, "type": "non-regular"})
    submodules = _run_bytes(("git", "submodule", "status", "--recursive"), root)
    material = {
        "commit": commit,
        "branch": branch,
        "tracked_diff_sha256": _sha256_bytes(diff),
        "tracked_diff_bytes": len(diff),
        "hidden_index_flags": hidden_index_flags,
        "untracked": untracked,
        "submodule_status_sha256": _sha256_bytes(submodules),
        "submodule_status": submodules.decode("utf-8", "replace").splitlines(),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    material["state_sha256"] = _sha256_bytes(encoded)
    material["clean"] = (
        not diff
        and not hidden_index_flags
        and not untracked
        and not submodules.strip()
    )
    return material


def _named_source_state(root: Path, repository: str) -> Dict[str, Any]:
    state = _source_state(root)
    return {
        "repository": repository,
        "root": str(root.resolve()),
        **state,
    }


def _require_clean_sources(sources: Dict[str, Dict[str, Any]]) -> None:
    labels = {"twin": "Twin", "firmware": "Firmware"}
    for name in ("twin", "firmware"):
        source = sources[name]
        if not source.get("clean"):
            hidden = source.get("hidden_index_flags")
            detail = ""
            if hidden:
                detail = "; hidden Git index flags: %s" % ", ".join(
                    "%s:%s" % (entry["tag"], entry["path"])
                    for entry in hidden
                )
            raise ValueError(
                "%s repository must be clean for full acceptance%s"
                % (labels[name], detail)
            )


class _Tee:
    def __init__(self, streams: Iterable[TextIO]) -> None:
        self.streams = tuple(streams)

    def write(self, payload: str) -> int:
        for stream in self.streams:
            stream.write(payload)
        return len(payload)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def writeln(self, payload: str = "") -> None:
        self.write(payload + "\n")


class _RecordingResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.test_ids: List[str] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.test_ids.append(test.id())
        super().startTest(test)


def _test_suite(args: argparse.Namespace) -> int:
    start_dir = Path(args.start_dir).resolve()
    summary_path = Path(args.summary).resolve()
    summary_path.unlink(missing_ok=True)
    log_path = Path(args.log).resolve() if args.log else None
    log_stream: Optional[TextIO] = None
    streams: List[TextIO] = [sys.stdout]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log_path.open("w", encoding="utf-8")
        streams.append(log_stream)

    started_at = _utc_now()
    started = time.monotonic()
    try:
        suite = unittest.defaultTestLoader.discover(
            str(start_dir), pattern=args.pattern
        )
        runner = unittest.TextTestRunner(
            stream=_Tee(streams),
            verbosity=2,
            resultclass=_RecordingResult,
        )
        result = runner.run(suite)
    finally:
        if log_stream is not None:
            log_stream.close()

    test_ids = sorted(result.test_ids)
    missing = [required for required in args.require_test if required not in test_ids]
    skipped = len(result.skipped)
    failed = len(result.failures)
    errors = len(result.errors)
    expected_failures = len(result.expectedFailures)
    unexpected_successes = len(result.unexpectedSuccesses)
    passed = (
        result.testsRun
        - skipped
        - failed
        - errors
        - expected_failures
        - unexpected_successes
    )
    skip_reasons: Dict[str, int] = {}
    skipped_tests: Dict[str, str] = {}
    for skipped_test, reason in result.skipped:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        skipped_tests[skipped_test.id()] = reason
    expected_skip_reasons: Dict[str, int] = {}
    for declaration in args.expect_skip_reason:
        count_text, separator, reason = declaration.partition(":")
        if not separator or not count_text.isdigit() or not reason:
            raise ValueError(
                "--expect-skip-reason must have the form COUNT:EXACT_REASON"
            )
        if reason in expected_skip_reasons:
            raise ValueError("duplicate expected skip reason: %s" % reason)
        expected_skip_reasons[reason] = int(count_text)
    skip_reasons_match = skip_reasons == expected_skip_reasons
    expected_skipped_tests: Dict[str, str] = {}
    if args.skip_policy == "source-without-qemu":
        expected_skipped_tests = SOURCE_WITHOUT_QEMU_SKIPS
    skipped_tests_match = (
        True if args.skip_policy == "none" else skipped_tests == expected_skipped_tests
    )
    gate_pass = bool(
        result.wasSuccessful()
        and expected_failures == 0
        and unexpected_successes == 0
        and result.testsRun >= args.min_tests
        and skipped == args.expect_skips
        and skip_reasons_match
        and skipped_tests_match
        and not missing
    )
    qemu_environment: Dict[str, Any] = {}
    configured_qemu = os.environ.get("P210_QEMU_BINARY")
    if configured_qemu:
        qemu_path = Path(configured_qemu)
        qemu_environment["configured_path"] = str(qemu_path)
        qemu_environment["available"] = qemu_path.is_file()
        if qemu_path.is_file():
            qemu_environment.update(_artifact(qemu_path))
    configured_build = os.environ.get("P210_QEMU_BUILD_DIR")
    if configured_build:
        build_path = Path(configured_build)
        qemu_environment["build_dir"] = str(build_path)
        compile_commands = build_path / "compile_commands.json"
        qemu_environment["compile_commands_available"] = compile_commands.is_file()
        if compile_commands.is_file():
            qemu_environment["compile_commands"] = _artifact(compile_commands)

    summary: Dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "unittest-gate",
        "label": args.label,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "gate_pass": gate_pass,
        "requirements": {
            "expected_skips": args.expect_skips,
            "expected_skip_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(expected_skip_reasons.items())
            ],
            "skip_policy": args.skip_policy,
            "expected_skipped_tests": expected_skipped_tests,
            "minimum_tests": args.min_tests,
            "required_tests": sorted(args.require_test),
            "missing_required_tests": missing,
        },
        "results": {
            "tests_run": result.testsRun,
            "passed": passed,
            "failures": failed,
            "errors": errors,
            "skipped": skipped,
            "expected_failures": expected_failures,
            "unexpected_successes": unexpected_successes,
            "skip_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(skip_reasons.items())
            ],
            "skipped_tests": skipped_tests,
        },
        "tests": test_ids,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "qemu_environment": qemu_environment,
    }
    _write_json(summary_path, summary)
    state = "PASS" if gate_pass else "FAIL"
    print(
        "TEST_GATE %s %s run=%d passed=%d skipped=%d expected_skips=%d"
        % (args.label, state, result.testsRun, passed, skipped, args.expect_skips)
    )
    for reason, count in sorted(skip_reasons.items()):
        print("TEST_GATE SKIP count=%d reason=%s" % (count, reason))
    if missing:
        print("TEST_GATE MISSING required=%s" % ",".join(missing), file=sys.stderr)
    if not skip_reasons_match:
        print("TEST_GATE SKIP_REASONS do not match the declared budget", file=sys.stderr)
    if not skipped_tests_match:
        print("TEST_GATE SKIPPED_TESTS do not match the declared policy", file=sys.stderr)
    return 0 if gate_pass else 1


def _start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    firmware_root = Path(args.firmware_root).resolve()
    output = Path(args.output).resolve()
    twin_source = _named_source_state(root, "Atom-NeptuneSDR-Twin")
    firmware_source = _named_source_state(firmware_root, "Atom-NeptuneSDR-Firmware")
    sources = {
        "twin": twin_source,
        "firmware": firmware_source,
    }
    if args.mode == "full":
        _require_clean_sources(sources)
    output.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema": SCHEMA,
        "kind": "acceptance-run-state",
        "status": "RUNNING",
        "mode": args.mode,
        "run_id": args.run_id,
        "started_at": _utc_now(),
        # Keep ``source`` as the Twin alias for v1 manifest consumers while
        # making the two-repository execution boundary explicit.
        "source": twin_source,
        "sources": sources,
    }
    _write_json(output / "run-state.json", payload)
    return 0


def _load_passing_summary(path: Path, label: str) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA or not payload.get("gate_pass"):
        raise ValueError("%s test summary is not a passing gate: %s" % (label, path))
    return payload


def _artifact(path: Path, base: Optional[Path] = None) -> Dict[str, Any]:
    resolved = path.resolve()
    name = str(resolved)
    if base is not None:
        try:
            name = str(resolved.relative_to(base.resolve()))
        except ValueError:
            pass
    return {
        "path": name,
        "bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
    }


def _build_cache_inputs(args: argparse.Namespace) -> Dict[str, Any]:
    sources = {
        "twin": _named_source_state(Path(args.root), "Atom-NeptuneSDR-Twin"),
        "firmware": _named_source_state(
            Path(args.firmware_root), "Atom-NeptuneSDR-Firmware"
        ),
    }
    artifact_paths = {
        "qemu": Path(args.qemu).resolve(),
        "guest_fft_streamer": Path(args.guest).resolve(),
        "host_iio_info": Path(args.iio_info).resolve(),
        "host_iio_readdev": Path(args.iio_readdev).resolve(),
        "host_libiio": Path(args.libiio).resolve(),
    }
    executable_artifacts = (
        "qemu",
        "guest_fft_streamer",
        "host_iio_info",
        "host_iio_readdev",
    )
    for label in executable_artifacts:
        path = artifact_paths[label]
        if not path.is_file() or not os.access(str(path), os.X_OK):
            raise ValueError("%s cache artifact is not executable: %s" % (label, path))
    if not artifact_paths["host_libiio"].is_file():
        raise ValueError(
            "host_libiio cache artifact is not a regular file: %s"
            % artifact_paths["host_libiio"]
        )
    return {
        "sources": sources,
        "artifacts": {
            label: _artifact(path) for label, path in artifact_paths.items()
        },
    }


def _write_build_cache(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    inputs = _build_cache_inputs(args)
    payload = {
        "schema": BUILD_CACHE_SCHEMA,
        "kind": "p210-runtime-build-cache-binding",
        "created_at": _utc_now(),
        **inputs,
    }
    _write_json(output, payload)
    print("P210_BUILD_CACHE BOUND path=%s" % output)
    return 0


def _verify_build_cache(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    recorded = json.loads(output.read_text(encoding="utf-8"))
    if (
        recorded.get("schema") != BUILD_CACHE_SCHEMA
        or recorded.get("kind") != "p210-runtime-build-cache-binding"
    ):
        raise ValueError("P210 build cache binding has the wrong schema or kind")

    current = _build_cache_inputs(args)
    recorded_sources = recorded.get("sources")
    recorded_artifacts = recorded.get("artifacts")
    if not isinstance(recorded_sources, dict) or not isinstance(
        recorded_artifacts, dict
    ):
        raise ValueError("P210 build cache binding is incomplete")

    for name, source in current["sources"].items():
        cached = recorded_sources.get(name)
        if not isinstance(cached, dict):
            raise ValueError("P210 build cache has no %s source identity" % name)
        for field in ("repository", "commit", "state_sha256", "clean"):
            if cached.get(field) != source[field]:
                raise ValueError(
                    "P210 build cache %s source %s does not match current source"
                    % (name, field)
                )

    for name, artifact in current["artifacts"].items():
        cached = recorded_artifacts.get(name)
        if not isinstance(cached, dict):
            raise ValueError("P210 build cache has no %s artifact identity" % name)
        for field in ("bytes", "sha256"):
            if cached.get(field) != artifact[field]:
                raise ValueError(
                    "P210 build cache %s artifact %s does not match current artifact"
                    % (name, field)
                )

    print("P210_BUILD_CACHE VERIFIED path=%s" % output)
    return 0


def _verify_runtime_contract(
    runtime: Path, runtime_manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """Independently prove the high-value firmware claims from retained data."""

    execution = runtime_manifest.get("execution_target", {})
    if not runtime_manifest.get("abi_compatible"):
        raise ValueError("runtime manifest is not ABI-compatible")
    if execution.get("machine") != "xilinx-zynq-a9,p210=on":
        raise ValueError("runtime manifest names the wrong QEMU machine")
    if execution.get("cpu_count") != 2 or execution.get("memory_bytes") != 536_870_912:
        raise ValueError("runtime manifest has the wrong Zynq CPU/memory profile")

    iio_text = (runtime / "p210-qemu-iio-info.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    iio_markers = (
        "iio_info version: 0.26 (git tag:a0eca0d)",
        "Backend version: 0.26 (git tag: v0.26)",
        "IIO context has 5 devices:",
        "iio:device0: ad9361-phy",
        "cf-ad9361-lpc (buffer capable)",
        "rf_bandwidth value: 50000000",
        "sampling_frequency value: 61440000",
    )
    missing_iio = [marker for marker in iio_markers if marker not in iio_text]
    if missing_iio:
        raise ValueError("retained IIOD evidence is incomplete: %s" % missing_iio[0])

    serial_text = (runtime / "p210-qemu.log").read_text(
        encoding="utf-8", errors="replace"
    )
    serial_markers = (
        "AD936x Rev 2 successfully initialized",
        "NEPTUNE_RUNTIME cpu-online=0-1",
        "NEPTUNE_FFT accelerator-id=5446464e version=00010000 caps=0000003f",
        "NEPTUNE_FFT rf-bandwidth=50000000 sample-rate=61440000",
        "NEPTUNE_FFT ready port=30432 n=65536 channels=2 input=iio-dmac-cpu-copy",
        "macb e000b000.ethernet eth0: link up",
        "bins=131072 bytes=262288",
    )
    missing_serial = [marker for marker in serial_markers if marker not in serial_text]
    if missing_serial:
        raise ValueError("retained guest log is incomplete: %s" % missing_serial[0])

    report = json.loads(
        (runtime / "p210-qemu-fft-report.json").read_text(encoding="utf-8")
    )
    required_report = {
        "status": "passed",
        "transport": "guest-arm-nsft-v1-tcp",
        "sample_rate_hz": 61_440_000,
        "center_frequency_hz": 2_400_000_000,
        "wire_bytes_received": 262_288,
        "crc_checked": True,
        "tone_contract_checked": True,
    }
    for key, expected in required_report.items():
        if report.get(key) != expected:
            raise ValueError("FFT report %s does not equal %r" % (key, expected))
    if report.get("socket_bytes_received", 0) < report["wire_bytes_received"]:
        raise ValueError("FFT report socket byte count is inconsistent")
    if (runtime / "p210-qemu-fft.nsft").stat().st_size != 262_288:
        raise ValueError("retained NSFT capture is not one complete two-channel update")

    channels = report.get("channels")
    if not isinstance(channels, list) or len(channels) != 2:
        raise ValueError("FFT report does not contain exactly two channels")
    by_channel = {item.get("channel"): item for item in channels if isinstance(item, dict)}
    if set(by_channel) != {0, 1}:
        raise ValueError("FFT report channel identities are not 0 and 1")
    expected_peaks = {0: (5_120, -2.53), 1: (13_312, -6.08)}
    for channel, (peak_bin, peak_dbfs) in expected_peaks.items():
        item = by_channel[channel]
        expected_fields = {
            "fft_size": 65_536,
            "bins": 65_536,
            "encoding": "UINT16_LOG_POWER",
            "sample_rate_hz": 61_440_000,
            "center_frequency_hz": 2_400_000_000,
            "peak_bin": peak_bin,
        }
        for key, expected in expected_fields.items():
            if item.get(key) != expected:
                raise ValueError(
                    "FFT channel %d %s does not equal %r" % (channel, key, expected)
                )
        measured_power = item.get("peak_dbfs")
        if not isinstance(measured_power, (int, float)) or isinstance(
            measured_power, bool
        ):
            raise ValueError("FFT channel %d power is not numeric" % channel)
        if abs(float(measured_power) - peak_dbfs) > 0.10:
            raise ValueError("FFT channel %d power is outside tolerance" % channel)
        for key in ("sequence", "timestamp_ns", "config_epoch"):
            if item.get(key) != report.get(key):
                raise ValueError("FFT channel %d %s is not synchronized" % (channel, key))

    return {
        "machine": execution["machine"],
        "cpu_count": execution["cpu_count"],
        "memory_bytes": execution["memory_bytes"],
        "iiod_version": "0.26",
        "iio_devices": 5,
        "rx_bandwidth_hz": 50_000_000,
        "sample_rate_hz": 61_440_000,
        "fft_size": 65_536,
        "channels": 2,
        "peak_bins": [by_channel[0]["peak_bin"], by_channel[1]["peak_bin"]],
        "nsft_wire_bytes": report["wire_bytes_received"],
        "crc_checked": True,
    }


def _finish_full(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    firmware_root = Path(args.firmware_root).resolve()
    output = Path(args.output).resolve()
    state_path = output / "run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA or state.get("status") != "RUNNING":
        raise ValueError("acceptance run state is absent or not RUNNING")
    if state.get("mode") != "full":
        raise ValueError("only a full run can produce a full acceptance manifest")
    recorded_sources = state.get("sources")
    if not isinstance(recorded_sources, dict):
        raise ValueError("acceptance run state has no two-repository source identity")
    current_source = _named_source_state(root, "Atom-NeptuneSDR-Twin")
    current_firmware_source = _named_source_state(
        firmware_root, "Atom-NeptuneSDR-Firmware"
    )
    current_sources = {
        "twin": current_source,
        "firmware": current_firmware_source,
    }
    for name, current in current_sources.items():
        recorded = recorded_sources.get(name)
        if not isinstance(recorded, dict):
            raise ValueError("acceptance run state is missing %s source" % name)
        if (
            recorded.get("repository") != current["repository"]
            or recorded.get("root") != current["root"]
        ):
            raise ValueError(
                "%s repository identity changed during acceptance" % name.capitalize()
            )
        if current["state_sha256"] != recorded.get("state_sha256"):
            raise ValueError(
                "%s repository source state changed during acceptance" % name.capitalize()
            )
    _require_clean_sources(current_sources)

    reference_summary = _load_passing_summary(
        Path(args.reference_summary), "reference"
    )
    cosim_summary = _load_passing_summary(Path(args.cosim_summary), "cosim")
    firmware_summary = _load_passing_summary(
        Path(args.firmware_summary), "firmware"
    )
    if reference_summary["results"]["skipped"] != 0:
        raise ValueError("full reference suite contains skipped tests")
    if cosim_summary["results"]["skipped"] != 0:
        raise ValueError("full co-simulation suite contains skipped tests")
    if firmware_summary["results"]["skipped"] != 0:
        raise ValueError("full Firmware suite contains skipped tests")

    firmware_log = Path(args.firmware_log).resolve()
    firmware_lines = firmware_log.read_text(encoding="utf-8", errors="replace").splitlines()
    if PASS_MARKER not in firmware_lines:
        raise ValueError("firmware runtime PASS marker is absent")

    qemu = Path(args.qemu).resolve(strict=True)
    qemu_build = Path(args.qemu_build_dir).resolve(strict=True)
    configured_qemu = (qemu_build / "qemu-system-arm").resolve(strict=True)
    if not os.path.samefile(str(qemu), str(configured_qemu)):
        raise ValueError("QEMU binary and configured build directory do not match")
    tested_qemu = cosim_summary.get("qemu_environment", {})
    if not tested_qemu.get("available"):
        raise ValueError("co-simulation summary has no executable QEMU identity")
    if tested_qemu.get("sha256") != _sha256_file(qemu):
        raise ValueError("QEMU changed after the live co-simulation suite")
    tested_commands = tested_qemu.get("compile_commands", {})
    compile_commands = qemu_build / "compile_commands.json"
    if tested_commands.get("sha256") != _sha256_file(compile_commands):
        raise ValueError("QEMU compile database changed after co-simulation")
    version_output = subprocess.check_output(
        (str(qemu), "--version"), stderr=subprocess.STDOUT, text=True
    ).splitlines()
    if not version_output or "QEMU emulator version 10.0.2" not in version_output[0]:
        raise ValueError("full acceptance requires pinned QEMU 10.0.2")

    runtime = Path(args.runtime_dir).resolve(strict=True)
    required_runtime = (
        "runtime-manifest.json",
        "p210-kernel.bin",
        "p210-devicetree.dtb",
        "qemu-fft-runtime.cpio.gz",
        "p210-qemu.log",
        "p210-qemu-iio-info.txt",
        "p210-qemu-fft.nsft",
        "p210-qemu-fft-report.json",
    )
    missing_runtime = [name for name in required_runtime if not (runtime / name).is_file()]
    if missing_runtime:
        raise ValueError("runtime evidence is incomplete: %s" % ", ".join(missing_runtime))
    runtime_manifest = json.loads(
        (runtime / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    runtime_firmware = runtime_manifest.get("firmware_source")
    if not isinstance(runtime_firmware, dict):
        raise ValueError("runtime manifest has no Firmware source identity")
    expected_firmware_identity = {
        "repository": "Atom-NeptuneSDR-Firmware",
        "commit": current_firmware_source["commit"],
        "state_sha256": current_firmware_source["state_sha256"],
    }
    for key, expected in expected_firmware_identity.items():
        if runtime_firmware.get(key) != expected:
            raise ValueError(
                "runtime manifest Firmware %s does not match tested source" % key
            )
    verified_claims = _verify_runtime_contract(runtime, runtime_manifest)
    runtime_artifacts = [
        _artifact(path, output)
        for path in sorted(runtime.rglob("*"))
        if path.is_file() and not path.name.endswith(".part")
    ]

    evidence_artifacts = []
    excluded = {
        state_path.resolve(),
        (output / "status").resolve(),
        (output / "acceptance-manifest.json").resolve(),
    }
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.resolve() not in excluded and not path.name.endswith(".part"):
            if runtime in path.parents:
                continue
            evidence_artifacts.append(_artifact(path, output))

    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "full-virtual-twin-acceptance",
        "status": "PASS",
        "mode": "full",
        "run_id": state["run_id"],
        "started_at": state["started_at"],
        "completed_at": _utc_now(),
        "source": current_source,
        "sources": current_sources,
        "tests": {
            "reference": reference_summary,
            "cosim": cosim_summary,
            "firmware": firmware_summary,
            "total_run": reference_summary["results"]["tests_run"]
            + cosim_summary["results"]["tests_run"]
            + firmware_summary["results"]["tests_run"],
            "total_skipped": 0,
        },
        "qemu": {
            **_artifact(qemu),
            "configured_path": str(Path(args.qemu).absolute()),
            "resolved_path": str(qemu),
            "build_dir": str(qemu_build),
            "version": version_output[0],
            "compile_commands": _artifact(qemu_build / "compile_commands.json"),
        },
        "firmware": {
            "pass": True,
            "pass_marker": PASS_MARKER,
            "verified_claims": verified_claims,
            "firmware_source": current_firmware_source,
            "runtime_directory": str(runtime),
            "runtime_manifest": runtime_manifest,
            "runtime_artifacts": runtime_artifacts,
            "launcher_log": _artifact(firmware_log, output),
        },
        "evidence_artifacts": evidence_artifacts,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
    }
    _write_json(output / "acceptance-manifest.json", manifest)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    tests = subparsers.add_parser("test-suite")
    tests.add_argument("--start-dir", required=True)
    tests.add_argument("--pattern", default="test*.py")
    tests.add_argument("--label", required=True)
    tests.add_argument("--summary", required=True)
    tests.add_argument("--log")
    tests.add_argument("--expect-skips", type=int, required=True)
    tests.add_argument("--expect-skip-reason", action="append", default=[])
    tests.add_argument(
        "--skip-policy",
        choices=("none", "source-without-qemu"),
        default="none",
    )
    tests.add_argument("--min-tests", type=int, default=1)
    tests.add_argument("--require-test", action="append", default=[])
    tests.set_defaults(function=_test_suite)

    for command, function in (
        ("write-build-cache", _write_build_cache),
        ("verify-build-cache", _verify_build_cache),
    ):
        cache = subparsers.add_parser(command)
        cache.add_argument("--root", required=True)
        cache.add_argument("--firmware-root", required=True)
        cache.add_argument("--qemu", required=True)
        cache.add_argument("--guest", required=True)
        cache.add_argument("--iio-info", required=True)
        cache.add_argument("--iio-readdev", required=True)
        cache.add_argument("--libiio", required=True)
        cache.add_argument("--output", required=True)
        cache.set_defaults(function=function)

    start = subparsers.add_parser("start")
    start.add_argument("--root", required=True)
    start.add_argument("--firmware-root", required=True)
    start.add_argument("--output", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--mode", choices=("source", "full"), required=True)
    start.set_defaults(function=_start)

    finish = subparsers.add_parser("finish-full")
    finish.add_argument("--root", required=True)
    finish.add_argument("--firmware-root", required=True)
    finish.add_argument("--output", required=True)
    finish.add_argument("--reference-summary", required=True)
    finish.add_argument("--cosim-summary", required=True)
    finish.add_argument("--firmware-summary", required=True)
    finish.add_argument("--qemu", required=True)
    finish.add_argument("--qemu-build-dir", required=True)
    finish.add_argument("--firmware-log", required=True)
    finish.add_argument("--runtime-dir", required=True)
    finish.set_defaults(function=_finish_full)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.function(args))
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print("acceptance_gate.py: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
