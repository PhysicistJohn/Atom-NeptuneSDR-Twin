"""Command-line entry point for the executable twin and conformance tooling."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import sysconfig
import threading
import time
from typing import Optional, Sequence
import zipfile

from .board import NeptuneSDRTwin
from .contracts import ContractSystem
from .firmware import (
    fetch_locked_artifact,
    load_firmware_lock,
    validate_fit_image,
    validate_p210_firmware,
)
from .fft import FFTConfig, PLThroughputContract, PayloadEncoding, calculate_output_rate_budget
from .spec import P210Spec
from .throughput import Wideband50MHzProfile
from .usb import load_observed_usb_profile
from .version import __version__
from .xsa import validate_xsa
from .zynq import BootSource


def _json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _contract_path() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "specs" / "contracts.json"
    if source_tree.is_file():
        return source_tree
    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "neptunesdr-twin"
        / "contracts.json"
    )
    if installed.is_file():
        return installed
    raise FileNotFoundError("cannot locate the installed P210 contract system")


def _validate_firmware(path: Path):
    if path.suffix.lower() == ".xsa":
        return validate_xsa(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = {Path(name).name: name for name in archive.namelist()}
            if "pluto.frm" not in names:
                raise ValueError("firmware zip contains no pluto.frm")
            return validate_fit_image(archive.read(names["pluto.frm"]), str(path) + "!pluto.frm")
    if path.suffix.lower() in {".frm", ".dfu", ".itb"}:
        return validate_fit_image(path.read_bytes(), str(path))
    return validate_p210_firmware(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neptune-twin",
        description="Contract-driven executable twin and conformance harness for HAMGEEK P210",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("info", help="show the resolved listing/firmware specification")
    commands.add_parser("wideband", help="assess the explicit 50 MHz on-chip profile")
    fft_plan = commands.add_parser(
        "fft-plan", help="budget the large on-chip FFT and spectrum-result transport"
    )
    fft_plan.add_argument("--size", type=int, default=65_536)
    fft_plan.add_argument("--channels", type=int, choices=(1, 2), default=2)
    fft_plan.add_argument("--sample-rate", type=int, default=61_440_000)
    fft_plan.add_argument("--updates-per-second", type=float, default=20.0)
    fft_plan.add_argument("--bin-start", type=int, default=0)
    fft_plan.add_argument("--bin-count", type=int)
    fft_plan.add_argument(
        "--encoding", choices=("uint16", "float32"), default="uint16"
    )
    fft_plan.add_argument("--stream-clock", type=int, default=100_000_000)
    fft_plan.add_argument("--lanes", type=int, default=2)
    commands.add_parser("usb", help="show the observed Neptune-family USB descriptor contract")
    commands.add_parser("contracts", help="compose and check all assume/guarantee contracts")

    snapshot = commands.add_parser("snapshot", help="boot deterministically and emit state JSON")
    snapshot.add_argument("--boot-source", choices=[item.value for item in BootSource], default="qspi")
    snapshot.add_argument("--output", type=Path)

    serve = commands.add_parser("serve", help="serve the twin over the standard IIOD TCP protocol")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=30431)
    serve.add_argument("--duration", type=float, help="stop after this many seconds")
    serve.add_argument("--dry-run", action="store_true")

    validate = commands.add_parser("validate-firmware", help="inspect a P210 bundle or Pluto FIT/zip")
    validate.add_argument("path", type=Path)

    fetch = commands.add_parser("fetch-firmware", help="download one content-addressed firmware input")
    fetch.add_argument("name", choices=sorted(load_firmware_lock()["artifacts"]))
    fetch.add_argument("output", type=Path)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "info":
        spec = P210Spec.load_default()
        _json({"resolved": spec.summary(), "unknown_until_capture": list(spec.unknowns)})
        return 0
    if args.command == "wideband":
        _json(Wideband50MHzProfile().assess())
        return 0
    if args.command == "fft-plan":
        encoding = (
            PayloadEncoding.UINT16_LOG_POWER
            if args.encoding == "uint16"
            else PayloadEncoding.FLOAT32_DBFS
        )
        config = FFTConfig(
            fft_size=args.size,
            channels=args.channels,
            sample_rate_hz=args.sample_rate,
            update_rate_hz=args.updates_per_second,
            bin_start=args.bin_start,
            bin_count=args.bin_count,
            payload_encoding=encoding,
        )
        ingress = PLThroughputContract(
            stream_clock_hz=args.stream_clock,
            lanes=args.lanes,
            input_sample_rate_hz=args.sample_rate,
            channels=args.channels,
            result_fifo_updates=2,
        ).assess(config)
        egress = calculate_output_rate_budget(
            args.size,
            channels=args.channels,
            updates_per_second=config.effective_update_rate_hz,
            encoding=encoding,
            bin_start=args.bin_start,
            bin_count=args.bin_count,
        )
        _json(
            {
                "configuration": {
                    "fft_size": config.fft_size,
                    "channels": config.channels,
                    "sample_rate_hz": config.sample_rate_hz,
                    "bin_start": config.bin_start,
                    "bin_count": config.bin_count,
                    "bin_resolution_hz": config.sample_rate_hz / config.fft_size,
                    "frames_per_update": config.frames_per_update,
                    "effective_update_rate_hz": config.effective_update_rate_hz,
                    "encoding": config.payload_encoding.name,
                },
                "pl_ingress": ingress.to_dict(),
                "host_egress": egress.to_dict(),
                "packet_contract": "NSFT version 1, network byte order, CRC32",
                "synthesis_and_post_route_timing_required": True,
            }
        )
        return 0 if ingress.fits and egress.fits else 1
    if args.command == "usb":
        profile = load_observed_usb_profile()
        config = profile.parsed_configuration
        _json(
            {
                "basis": profile.capture["basis"],
                "device_descriptor_sha256": __import__("hashlib").sha256(
                    profile.device_descriptor
                ).hexdigest(),
                "configuration_descriptor_sha256": __import__("hashlib").sha256(
                    profile.configuration_descriptor
                ).hexdigest(),
                "vendor_id": profile.parsed_device.vendor_id,
                "product_id": profile.parsed_device.product_id,
                "bcd_device": profile.parsed_device.device_version,
                "interfaces": config.declared_interface_count,
                "endpoints": ["0x%02x" % value for value in config.endpoint_addresses],
                "max_power_ma": config.max_power_ma,
                "delivered_unit_confirmation_required": True,
            }
        )
        return 0
    if args.command == "contracts":
        system = ContractSystem.from_json(_contract_path())
        report = system.compose()
        _json(
            {
                "system": report.name,
                "compatible": report.ok,
                "components": len(system.contracts),
                "connections": len(system.connections),
                "assumption_bindings": len(report.bindings),
                "issues": [
                    {
                        "code": issue.code,
                        "severity": issue.severity.value,
                        "path": issue.path,
                        "message": issue.message,
                    }
                    for issue in report.issues
                ],
            }
        )
        return 0 if report.ok else 1
    if args.command == "snapshot":
        twin = NeptuneSDRTwin()
        twin.boot_to_userspace(BootSource(args.boot_source))
        if args.output:
            digest = twin.write_snapshot(args.output)
            _json({"path": str(args.output), "sha256": digest})
        else:
            _json(twin.snapshot())
        return 0
    if args.command == "serve":
        if args.dry_run:
            _json(
                {
                    "would_listen": "%s:%d" % (args.host, args.port),
                    "libiio_uri": "ip:%s" % args.host,
                    "boot_source": "qspi",
                }
            )
            return 0
        stop = threading.Event()

        def request_stop(signum=None, frame=None):
            stop.set()

        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        try:
            with NeptuneSDRTwin() as twin:
                twin.boot_to_userspace()
                address = twin.start_iiod(args.host, args.port)
                print("IIOD twin listening at ip:%s:%d" % address, flush=True)
                if args.duration is not None:
                    stop.wait(max(0.0, args.duration))
                else:
                    while not stop.wait(0.5):
                        pass
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    if args.command == "validate-firmware":
        report = _validate_firmware(args.path)
        _json(report.to_dict())
        return 0 if report.compatible else 1
    if args.command == "fetch-firmware":
        path = fetch_locked_artifact(args.name, args.output)
        _json({"artifact": args.name, "path": str(path), "verified": True})
        return 0
    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())
