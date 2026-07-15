#!/usr/bin/env python3
"""Prepare a read-only Zynq firmware smoke test; execute only with --run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
from typing import List, Optional


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPOSITORY / "src"
if str(SOURCE_TREE) not in sys.path:
    sys.path.insert(0, str(SOURCE_TREE))

from neptunesdr_twin.boot_harness import (  # noqa: E402
    DEFAULT_LOG_PATTERNS,
    build_qemu_command,
    extract_boot_artifacts,
    locate_qemu_system_arm,
    locked_artifact_path,
    run_qemu_boot,
    verify_locked_artifact,
)
from neptunesdr_twin.firmware import load_firmware_lock  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract firmware and print a bounded xilinx-zynq-a9 QEMU command. "
            "Dry-run is the default; --run is the only execution switch. No "
            "block device, host USB device, network backend, or flashing tool is used."
        )
    )
    parser.add_argument("source", nargs="?", type=Path, help="P210 tar, Pluto ZIP, or FIT .frm/.dfu")
    parser.add_argument("--artifact", help="use a named artifact already present in the locked cache")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPOSITORY / ".cache" / "firmware",
        help="locked firmware cache root",
    )
    parser.add_argument("--lock", type=Path, help="alternate firmware lock JSON")
    parser.add_argument("--work-dir", type=Path, help="directory for extracted kernel/DT/ramdisk")
    parser.add_argument("--configuration", help="FIT configuration node (for example config@1)")
    parser.add_argument("--qemu", help="explicit qemu-system-arm executable")
    parser.add_argument("--memory", type=int, default=512, metavar="MIB")
    parser.add_argument("--cpus", type=int, default=2, choices=(1, 2))
    parser.add_argument("--append", help="override the extracted/default kernel command line")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument(
        "--expect",
        action="append",
        metavar="REGEX",
        help="required boot-log regex; repeatable (default: Booting Linux and Linux version)",
    )
    parser.add_argument(
        "--reject",
        action="append",
        metavar="REGEX",
        help="fatal boot-log regex; repeatable (defaults include kernel panic/root mount failure)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="actually execute QEMU; without this switch only the command is printed",
    )
    parser.add_argument("--json", action="store_true", help="emit result metadata as JSON")
    return parser


def _resolve_source(args: argparse.Namespace) -> Path:
    if bool(args.source) == bool(args.artifact):
        raise ValueError("provide exactly one source path or --artifact NAME")
    if args.source:
        return args.source
    lock = load_firmware_lock(args.lock)
    artifacts = lock.get("artifacts", {})
    if args.artifact not in artifacts:
        raise ValueError("unknown locked artifact %r" % args.artifact)
    entry = artifacts[args.artifact]
    kind = entry.get("kind") if isinstance(entry, dict) else None
    if kind not in ("p210-sd-boot-tar", "official-pluto-release-zip"):
        raise ValueError(
            "locked artifact %r (%s) is evidence, not a direct-boot firmware input"
            % (args.artifact, kind or "unknown kind")
        )
    path = locked_artifact_path(args.artifact, args.cache_dir, args.lock)
    if not path.is_file():
        raise FileNotFoundError("locked artifact is not cached; run scripts/fetch_firmware.py first: %s" % path)
    verify_locked_artifact(args.artifact, path, args.lock)
    return path


def _default_work_dir(source: Path) -> Path:
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:12]
    return REPOSITORY / ".cache" / "qemu-boot" / (source.name + "-" + digest)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = _resolve_source(args)
        if not source.exists():
            raise FileNotFoundError(source)
        work_dir = args.work_dir or _default_work_dir(source)
        artifacts = extract_boot_artifacts(source, work_dir, args.configuration)
        located = locate_qemu_system_arm(args.qemu, required=args.run)
        qemu = str(located) if located is not None else (args.qemu or "qemu-system-arm")
        command = build_qemu_command(
            artifacts,
            qemu,
            memory_mib=args.memory,
            cpus=args.cpus,
            append=args.append,
        )
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    metadata = {
        "mode": "run" if args.run else "dry-run",
        "source": str(source.resolve()),
        "kind": artifacts.kind,
        "execution_scope": artifacts.execution_scope,
        "configuration": artifacts.configuration,
        "kernel": str(artifacts.kernel.resolve()),
        "devicetree": str(artifacts.devicetree.resolve()),
        "ramdisk": str(artifacts.ramdisk.resolve()) if artifacts.ramdisk else None,
        "hashes": dict(artifacts.hashes),
        "non_emulated_components": artifacts.non_emulated_components,
        "command": command,
        "limitations": [
            "QEMU does not execute the FPGA image or validate AD9361/DMA RF throughput.",
            "No host USB device is attached, so USB gadget enumeration is not a runtime claim.",
        ],
    }
    if artifacts.ramdisk is None:
        metadata["limitations"].append(
            "This bundle has no root filesystem; the smoke test can prove kernel entry only."
        )
    if not args.run:
        if args.json:
            print(json.dumps(metadata, indent=2, sort_keys=True))
        else:
            if located is None:
                print("note: qemu-system-arm was not found; command is a dry-run", file=sys.stderr)
            print("note: execution scope is %s" % artifacts.execution_scope, file=sys.stderr)
            print(
                "note: FPGA/RF datapath and host USB behavior are outside this QEMU smoke test",
                file=sys.stderr,
            )
            print(shlex.join(command))
        return 0

    patterns = tuple(args.expect) if args.expect else DEFAULT_LOG_PATTERNS
    try:
        run_options = {"timeout": args.timeout, "patterns": patterns}
        if args.reject is not None:
            run_options["reject_patterns"] = tuple(args.reject)
        result = run_qemu_boot(command, **run_options)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    metadata["boot"] = {
        "passed": result.passed,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "matched_patterns": result.matched_patterns,
        "missing_patterns": result.missing_patterns,
        "matched_rejections": result.matched_rejections,
        "output": result.output,
        "output_truncated": result.output_truncated,
    }
    if args.json:
        print(json.dumps(metadata, indent=2, sort_keys=True))
    else:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
        print(
            "%s smoke %s; matched %d/%d expected patterns%s"
            % (
                artifacts.execution_scope,
                "passed" if result.passed else "failed",
                len(result.matched_patterns),
                len(result.expected_patterns),
                " (bounded timeout reached)" if result.timed_out else "",
            ),
            file=sys.stderr,
        )
        if result.matched_rejections:
            print(
                "fatal log pattern(s): %s" % ", ".join(result.matched_rejections),
                file=sys.stderr,
            )
        if result.output_truncated:
            print("captured QEMU output exceeded the safety cap", file=sys.stderr)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
