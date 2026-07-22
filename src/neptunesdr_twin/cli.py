"""Command-line entry point for the executable twin and conformance tooling."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import math
from pathlib import Path
import signal
import sys
import sysconfig
import threading
import time
from typing import Callable, Optional, Sequence

from .ad9361 import AD9361, ENSMState
from .board import NeptuneSDRTwin
from .contracts import ContractSystem
from .fft import FFTConfig, PLThroughputContract, PayloadEncoding, calculate_output_rate_budget
from .spec import P210Spec
from .throughput import Wideband50MHzProfile
from .usb import load_observed_usb_profile
from .version import __version__
from .zynq import BootSource


def _json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _distribution_contract_path() -> Optional[Path]:
    """Locate data_files relative to the installed distribution, not Python."""

    try:
        distribution = metadata.distribution("neptunesdr-twin")
    except metadata.PackageNotFoundError:
        return None
    target_relative = (
        Path(distribution.locate_file(""))
        / "share"
        / "neptunesdr-twin"
        / "contracts.json"
    )
    if target_relative.is_file():
        return target_relative
    for item in distribution.files or ():
        if tuple(item.parts[-3:]) != ("share", "neptunesdr-twin", "contracts.json"):
            continue
        candidate = Path(distribution.locate_file(item))
        if candidate.is_file():
            return candidate
    return None


def _contract_path() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "specs" / "contracts.json"
    if source_tree.is_file():
        return source_tree
    distribution_data = _distribution_contract_path()
    if distribution_data is not None:
        return distribution_data
    installed = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "neptunesdr-twin"
        / "contracts.json"
    )
    if installed.is_file():
        return installed
    raise FileNotFoundError("cannot locate the installed P210 contract system")


def _host_port(value: str):
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("endpoint must be HOST:PORT") from exc
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("endpoint must contain a host and port in [1, 65535]")
    return host, port


def _listener_port(value: str) -> int:
    try:
        port = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be a decimal integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in [0, 65535]")
    return port


def _duration(value: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("duration must be a number") from exc
    if not math.isfinite(duration) or duration < 0:
        raise argparse.ArgumentTypeError("duration must be finite and non-negative")
    return duration


def _wait_for_stop(
    stop: threading.Event,
    duration_s: Optional[float],
    health_check: Optional[Callable[[], None]] = None,
) -> None:
    """Wait interruptibly while checking a service's health at bounded intervals."""

    deadline = None if duration_s is None else time.monotonic() + duration_s
    while True:
        if health_check is not None:
            health_check()
        if stop.is_set():
            return
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            interval = min(0.1, remaining)
        else:
            interval = 0.5
        if stop.wait(interval):
            return


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
    serve.add_argument("--port", type=_listener_port, default=30431)
    serve.add_argument("--duration", type=_duration, help="stop after this many seconds")
    serve.add_argument("--dry-run", action="store_true")

    usbip = commands.add_parser(
        "usbip-serve",
        help="export the observed composite USB device through standard USB/IP",
    )
    usbip.add_argument("--host", default="127.0.0.1")
    usbip.add_argument("--port", type=_listener_port, default=3240)
    usbip.add_argument(
        "--iiod-backend",
        type=_host_port,
        metavar="HOST:PORT",
        help="bridge native-IIO pipes to a real IIOD service instead of the local model",
    )
    usbip.add_argument("--duration", type=_duration, help="stop after this many seconds")
    usbip.add_argument("--dry-run", action="store_true")

    appliance = commands.add_parser(
        "appliance",
        help="run the complete local IIO, USB/IP and continuous 2x2 FFT appliance",
    )
    appliance.add_argument("--host", default="127.0.0.1")
    appliance.add_argument("--iiod-port", type=_listener_port, default=30431)
    appliance.add_argument("--spectrum-port", type=_listener_port, default=30432)
    appliance.add_argument("--usbip-port", type=_listener_port, default=3240)
    appliance.add_argument("--fft-size", type=int, default=65_536)
    appliance.add_argument("--sample-rate", type=int, default=61_440_000)
    appliance.add_argument("--bandwidth", type=int, default=50_000_000)
    appliance.add_argument("--center-frequency", type=int, default=2_400_000_000)
    appliance.add_argument("--updates-per-second", type=float, default=20.0)
    appliance.add_argument("--bin-start", type=int, default=0)
    appliance.add_argument("--bin-count", type=int)
    appliance.add_argument("--pending-updates", type=int, default=2)
    appliance.add_argument("--unpaced", action="store_true")
    appliance.add_argument("--no-default-tones", action="store_true")
    appliance.add_argument("--no-usbip", action="store_true")
    appliance.add_argument("--duration", type=_duration, help="stop after this many seconds")
    appliance.add_argument("--dry-run", action="store_true")

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
                    "libiio_uri": "ip:%s:%d" % (args.host, args.port),
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
                _wait_for_stop(stop, args.duration)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    if args.command == "usbip-serve":
        if args.dry_run:
            _json(
                {
                    "would_listen": "%s:%d" % (args.host, args.port),
                    "usbip_version": "1.1.1",
                    "busid": "1-1",
                    "native_iio": (
                        "tcp:%s:%d" % args.iiod_backend
                        if args.iiod_backend is not None
                        else "local-twin-context"
                    ),
                    "linux_attach": "usbip attach -r %s -b 1-1" % args.host,
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
                address = twin.start_usbip(
                    args.host,
                    args.port,
                    iiod_backend=args.iiod_backend,
                )
                print(
                    "NeptuneSDR USB/IP twin listening at %s:%d (busid 1-1)"
                    % address,
                    flush=True,
                )
                _wait_for_stop(stop, args.duration)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    if args.command == "appliance":
        requested = {
            "radio": {
                "center_frequency_hz": args.center_frequency,
                "sample_rate_hz": args.sample_rate,
                "rx_bandwidth_hz": args.bandwidth,
                "channels": 2,
            },
            "spectrum": {
                "fft_size": args.fft_size,
                "updates_per_second_ceiling": args.updates_per_second,
                "bin_start": args.bin_start,
                "bin_count": args.bin_count,
                "encoding": "UINT16_LOG_POWER",
                "packet": "paired NSFT-v1 over TCP",
            },
            "endpoints": {
                "iiod": "%s:%d" % (args.host, args.iiod_port),
                "spectrum": "%s:%d" % (args.host, args.spectrum_port),
                "usbip": None
                if args.no_usbip
                else "%s:%d" % (args.host, args.usbip_port),
                "usbip_busid": None if args.no_usbip else "1-1",
            },
            "default_tones": []
            if args.no_default_tones
            else [
                {"channel": 0, "offset_hz": 4_800_000, "amplitude": 1536},
                {"channel": 1, "offset_hz": 12_480_000, "amplitude": 1024},
            ],
            "continuous_dataflow": {
                "bounded_pending_updates": args.pending_updates,
                "realtime_pacing": not args.unpaced,
                "retune_epoch_atomic": True,
                "silent_drops": False,
                "raw_iq_reads": "exclusive RF owner; rejected while active",
            },
        }
        # Construct the same immutable FFT contract even on a dry run so bad
        # sizes, ranges, and egress selections fail before claiming readiness.
        config = FFTConfig(
            fft_size=args.fft_size,
            channels=2,
            window="rectangular",
            fftshift=False,
            update_rate_hz=args.updates_per_second,
            bin_start=args.bin_start,
            bin_count=args.bin_count,
            sample_rate_hz=args.sample_rate,
            center_frequency_hz=args.center_frequency,
            payload_encoding=PayloadEncoding.UINT16_LOG_POWER,
            full_scale=2048.0,
        )
        requested["spectrum"]["bin_count"] = config.bin_count
        if args.pending_updates <= 0:
            raise ValueError("pending-updates must be positive")
        if not AD9361.MIN_SAMPLE_RATE_HZ <= args.sample_rate <= AD9361.MAX_SAMPLE_RATE_HZ:
            raise ValueError("sample-rate is outside the AD9361 contact")
        if not AD9361.MIN_BANDWIDTH_HZ <= args.bandwidth <= AD9361.MAX_BANDWIDTH_HZ:
            raise ValueError("bandwidth is outside the AD9361 contact")
        if args.bandwidth > args.sample_rate:
            raise ValueError("bandwidth cannot exceed sample-rate")
        if not AD9361.MIN_CARRIER_HZ <= args.center_frequency <= AD9361.MAX_CARRIER_HZ:
            raise ValueError("center-frequency is outside the AD9361 RX contact")
        if not args.no_default_tones and any(
            abs(offset_hz) >= args.sample_rate / 2.0
            for offset_hz in (4_800_000, 12_480_000)
        ):
            raise ValueError(
                "default tones must be strictly inside Nyquist; increase sample-rate "
                "or pass --no-default-tones"
            )
        for name, port in (
            ("iiod-port", args.iiod_port),
            ("spectrum-port", args.spectrum_port),
            ("usbip-port", args.usbip_port),
        ):
            if not 0 <= port <= 65535:
                raise ValueError("%s must be in [0, 65535]" % name)
        selected_ports = [args.iiod_port, args.spectrum_port]
        if not args.no_usbip:
            selected_ports.append(args.usbip_port)
        fixed_ports = [port for port in selected_ports if port]
        if len(fixed_ports) != len(set(fixed_ports)):
            raise ValueError("nonzero appliance listener ports must be distinct")
        if args.dry_run:
            requested["status"] = "validated-dry-run"
            requested["effective_updates_per_second"] = config.effective_update_rate_hz
            requested["fft_frames_per_update"] = config.frames_per_update
            _json(requested)
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
                # AD9361 rejects a sample rate below either current bandwidth.
                # Narrow first when decreasing; widen only after the target
                # sample clock is active when increasing.
                if args.sample_rate < max(
                    twin.radio.rx_bandwidth_hz, twin.radio.tx_bandwidth_hz
                ):
                    twin.radio.set_rf_bandwidth("rx", args.bandwidth)
                    twin.radio.set_rf_bandwidth("tx", args.bandwidth)
                twin.radio.set_sample_rate(args.sample_rate)
                twin.radio.set_rf_bandwidth("rx", args.bandwidth)
                twin.radio.set_rf_bandwidth("tx", args.bandwidth)
                twin.radio.set_lo_frequency("rx", args.center_frequency)
                twin.radio.set_ensm_state(ENSMState.FDD)
                twin.advance(2_000_000)
                if not args.no_default_tones:
                    twin.rf.add_baseband_tone(0, 4_800_000, amplitude=1536)
                    twin.rf.add_baseband_tone(1, 12_480_000, amplitude=1024)
                twin.configure_fft(config)
                iiod_address = twin.start_iiod(args.host, args.iiod_port)
                spectrum_address = twin.start_spectrum_publisher(
                    args.host, args.spectrum_port
                )
                usbip_address = None
                if not args.no_usbip:
                    usbip_address = twin.start_usbip(args.host, args.usbip_port)
                runtime = twin.start_continuous_spectrum(
                    pending_update_capacity=args.pending_updates,
                    realtime_pacing=not args.unpaced,
                )
                if not runtime.wait_configured(1.0):
                    snapshot = runtime.snapshot()
                    if not runtime.running:
                        raise RuntimeError(
                            "continuous PL runtime stopped before readiness: %s"
                            % snapshot.get("last_error")
                        )
                    raise RuntimeError(
                        "continuous PL runtime did not configure before readiness deadline"
                    )
                ready = dict(requested)
                ready["status"] = "ready"
                ready["endpoints"] = {
                    "iiod": "%s:%d" % iiod_address,
                    "spectrum": "%s:%d" % spectrum_address,
                    "usbip": None
                    if usbip_address is None
                    else "%s:%d" % usbip_address,
                    "usbip_busid": None if usbip_address is None else "1-1",
                }
                ready["effective_updates_per_second"] = config.effective_update_rate_hz
                ready["fft_frames_per_update"] = config.frames_per_update
                _json(ready)

                def check_runtime() -> None:
                    if not runtime.running:
                        snapshot = runtime.snapshot()
                        raise RuntimeError(
                            "continuous PL runtime stopped: %s"
                            % snapshot.get("last_error")
                        )

                _wait_for_stop(stop, args.duration, check_runtime)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return 0
    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())
