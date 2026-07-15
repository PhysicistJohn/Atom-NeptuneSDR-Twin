#!/usr/bin/env python3
"""Assemble an ABI-audited P210 ARM runtime candidate; never flash hardware."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPOSITORY / "src"
if str(SOURCE_TREE) not in sys.path:
    sys.path.insert(0, str(SOURCE_TREE))

from neptunesdr_twin.boot_harness import (  # noqa: E402
    locked_artifact_path,
    verify_locked_artifact,
)
from neptunesdr_twin.firmware import load_firmware_lock  # noqa: E402
from neptunesdr_twin.runtime_rootfs import (  # noqa: E402
    build_iiod_probe_rootfs,
    build_qemu_fft_runtime_rootfs,
    build_qemu_tcp_probe_rootfs,
    build_qemu_tcp_rootfs,
    prepare_p210_runtime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Combine the locked public P210 kernel/device tree with the exact "
            "ADI Pluto v0.39 initramfs after auditing its ARM ABI, loader, "
            "libraries, iiod binary, and service contract. The result is an "
            "experimental runtime candidate, not vendor P210 firmware."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY / ".cache" / "p210-runtime",
        help="runtime output directory",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPOSITORY / ".cache" / "firmware",
        help="content-addressed firmware cache",
    )
    parser.add_argument("--lock", type=Path, help="alternate firmware lock JSON")
    parser.add_argument("--p210", type=Path, help="explicit public P210 boot tar")
    parser.add_argument("--pluto", type=Path, help="explicit official Pluto release ZIP")
    parser.add_argument(
        "--iiod-exec-probe",
        action="store_true",
        help="also create a test-only initramfs whose /init executes real iiod -V",
    )
    parser.add_argument(
        "--tcp-iiod",
        action="store_true",
        help=(
            "also create a test-only initramfs with network-only iiod on TCP 30431; "
            "this bypasses QEMU's missing P210 USB gadget controller"
        ),
    )
    parser.add_argument(
        "--tcp-probe-init",
        action="store_true",
        help=(
            "also create a fast test-only initramfs that configures QEMU GEM "
            "and execs the released ARM iiod as PID 1 on TCP 30431"
        ),
    )
    parser.add_argument(
        "--fft-streamer",
        type=Path,
        help=(
            "also create the hardware-test initramfs containing this static "
            "ARM EABI5 FFT streamer; Linux must be launched with mem=384M"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the full manifest")
    return parser


def _resolve_locked(
    explicit: Optional[Path],
    name: str,
    cache: Path,
    lock: Optional[Path],
) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(explicit)
        return explicit.resolve()
    path = locked_artifact_path(name, cache, lock)
    if not path.is_file():
        raise FileNotFoundError(
            "%s is not cached; run scripts/fetch_firmware.py %s first" % (path, name)
        )
    verify_locked_artifact(name, path, lock)
    return path.resolve()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        lock = load_firmware_lock(args.lock)
        p210 = _resolve_locked(args.p210, "p210-sd-boot", args.cache_dir, args.lock)
        pluto = _resolve_locked(args.pluto, "plutosdr-fw-v0.39", args.cache_dir, args.lock)
        p210_sha = _sha(p210)
        pluto_sha = _sha(pluto)
        candidate = prepare_p210_runtime(
            p210,
            pluto,
            args.output,
            provenance={
                "p210_source": str(p210),
                "p210_sha256": p210_sha,
                "pluto_source": str(pluto),
                "pluto_sha256": pluto_sha,
                "firmware_lock_schema": str(lock.get("schema", "unknown")),
            },
        )
        manifest = candidate.to_dict()
        derived: Dict[str, Dict[str, object]] = {}
        if args.iiod_exec_probe:
            path = build_iiod_probe_rootfs(
                candidate.artifacts.ramdisk,
                args.output / "qemu-iiod-exec-probe.cpio.gz",
            )
            derived["iiod_exec_probe"] = {
                "path": str(path.resolve()),
                "sha256": _sha(path),
                "purpose": "test-only real ARM dynamic-loader/iiod execution marker",
            }
        if args.tcp_iiod:
            path = build_qemu_tcp_rootfs(
                candidate.artifacts.ramdisk,
                args.output / "qemu-tcp-iiod.cpio.gz",
            )
            derived["tcp_iiod"] = {
                "path": str(path.resolve()),
                "sha256": _sha(path),
                "port": 30431,
                "purpose": "test-only network iiod independent of USB FunctionFS",
            }
        if args.tcp_probe_init:
            path = build_qemu_tcp_probe_rootfs(
                candidate.artifacts.ramdisk,
                args.output / "qemu-tcp-probe-init.cpio.gz",
            )
            derived["tcp_probe_init"] = {
                "path": str(path.resolve()),
                "sha256": _sha(path),
                "port": 30431,
                "guest_ipv4": "10.0.2.15",
                "purpose": "fast test-only GEM plus released ARM iiod PID-1 runtime",
            }
        if args.fft_streamer:
            path = build_qemu_fft_runtime_rootfs(
                candidate.artifacts.ramdisk,
                args.fft_streamer,
                args.output / "qemu-fft-runtime.cpio.gz",
            )
            derived["fft_runtime"] = {
                "path": str(path.resolve()),
                "sha256": _sha(path),
                "iiod_port": 30431,
                "spectrum_port": 30432,
                "linux_mem_limit": "384M",
                "dma_input_phys": "0x18000000",
                "dma_output_phys": "0x18100000",
                "streamer_sha256": _sha(args.fft_streamer),
                "purpose": "real ARM IIO-DMAC block, CPU copy, PL-FFT DMA, and NSFT/TCP runtime",
            }
            manifest["execution_target"] = {
                "machine": "xilinx-zynq-a9,p210=on",
                "cpu_count": 2,
                "memory_bytes": 512 * 1024 * 1024,
                "linux_memory_limit": "384M",
                "gem_phy_address": 0,
                "functional_devices": [
                    "AD9361 SPI control",
                    "CF-AXI ADC and DDS",
                    "four-entry AXI-DMAC",
                    "65,536-point two-channel PL FFT",
                    "Zynq GEM Ethernet",
                ],
                "not_executed": [
                    "P210 FPGA bitstream",
                    "physical AD9361 RF signal chain",
                    "USB gadget controller",
                ],
            }
        manifest["derived_test_images"] = derived
        manifest_path = args.output / "runtime-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print("runtime=%s" % args.output.resolve())
        print("manifest=%s" % manifest_path.resolve())
        print("classification=%s" % candidate.classification)
        print("kernel=%s" % candidate.kernel_name)
        print(
            "rootfs=Pluto %s libiio %s ARM-EABI%d-%s"
            % (
                candidate.rootfs.firmware_version,
                candidate.rootfs.libiio_version,
                candidate.rootfs.arm_eabi,
                candidate.rootfs.float_abi,
            )
        )
        print("released_iiod=%s" % candidate.rootfs.service_command)
        for name, item in sorted(derived.items()):
            print("%s=%s" % (name, item["path"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
