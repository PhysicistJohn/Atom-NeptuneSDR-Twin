#!/usr/bin/env python3
"""Receive and verify one two-channel NSFT update from ARM guest firmware."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time
from typing import Dict, List, Optional, Sequence


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPOSITORY / "src"
if str(SOURCE_TREE) not in sys.path:
    sys.path.insert(0, str(SOURCE_TREE))

from neptunesdr_twin.fft import SpectrumPacket  # noqa: E402
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder  # noqa: E402


EXPECTED_TONE_BINS = {0: 65_536 * 5 // 64, 1: 65_536 * 13 // 64}
EXPECTED_TONE_DBFS = {0: -2.53, 1: -6.08}
TONE_DBFS_TOLERANCE = 0.10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to the forwarded ARM FFT service, CRC-check NSFT-v1, "
            "and require a synchronized two-channel 65,536-bin update."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=30432)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output", type=Path, help="optionally retain received wire bytes")
    parser.add_argument(
        "--no-verify-tones",
        action="store_true",
        help="accept any peak bins instead of the QEMU RX tone contract",
    )
    return parser


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    error: Optional[OSError] = None
    while time.monotonic() < deadline:
        try:
            connection = socket.create_connection((host, port), timeout=1.0)
            connection.settimeout(1.0)
            return connection
        except OSError as exc:
            error = exc
            time.sleep(0.1)
    raise TimeoutError("FFT service did not accept TCP connections: %s" % error)


def _peak(packet: SpectrumPacket) -> Dict[str, object]:
    relative, value = max(enumerate(packet.values_dbfs), key=lambda item: item[1])
    bin_index = packet.bin_start + relative
    signed_bin = bin_index if bin_index <= packet.fft_size // 2 else bin_index - packet.fft_size
    offset_hz = signed_bin * packet.sample_rate_hz / packet.fft_size
    return {
        "channel": packet.channel,
        "sequence": packet.sequence,
        "fft_size": packet.fft_size,
        "bins": packet.bin_count,
        "sample_rate_hz": packet.sample_rate_hz,
        "center_frequency_hz": packet.center_frequency_hz,
        "peak_bin": bin_index,
        "peak_offset_hz": offset_hz,
        "peak_dbfs": value,
        "encoding": packet.encoding.name,
    }


def capture_update(
    host: str,
    port: int,
    timeout: float,
    *,
    output: Optional[Path] = None,
    verify_tones: bool = True,
) -> Dict[str, object]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    decoder = SpectrumStreamDecoder()
    by_sequence: Dict[int, Dict[int, SpectrumPacket]] = {}
    retained = bytearray()

    with _connect(host, port, deadline) as connection:
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(256 * 1024)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("FFT service closed before a complete update")
            retained.extend(chunk)
            for packet in decoder.feed(chunk):
                channels = by_sequence.setdefault(packet.sequence, {})
                channels[packet.channel] = packet
                if 0 in channels and 1 in channels:
                    selected: Sequence[SpectrumPacket] = (channels[0], channels[1])
                    if output is not None:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(bytes(retained))
                    results: List[Dict[str, object]] = []
                    for item in selected:
                        if item.fft_size != 65_536 or item.bin_count != 65_536:
                            raise RuntimeError("guest did not transmit full 65,536-bin spectra")
                        if item.sample_rate_hz != 61_440_000:
                            raise RuntimeError("guest spectrum sample rate is not 61.44 MSPS")
                        peak = _peak(item)
                        if verify_tones and peak["peak_bin"] != EXPECTED_TONE_BINS[item.channel]:
                            raise RuntimeError(
                                "channel %d peak bin %d does not match QEMU RX tone bin %d"
                                % (
                                    item.channel,
                                    peak["peak_bin"],
                                    EXPECTED_TONE_BINS[item.channel],
                                )
                            )
                        if verify_tones and abs(
                            peak["peak_dbfs"] - EXPECTED_TONE_DBFS[item.channel]
                        ) > TONE_DBFS_TOLERANCE:
                            raise RuntimeError(
                                "channel %d peak %.2f dBFS does not match QEMU RX tone %.2f dBFS"
                                % (
                                    item.channel,
                                    peak["peak_dbfs"],
                                    EXPECTED_TONE_DBFS[item.channel],
                                )
                            )
                        results.append(peak)
                    return {
                        "status": "passed",
                        "transport": "guest-arm-nsft-v1-tcp",
                        "sequence": selected[0].sequence,
                        "channels": results,
                        "wire_bytes_received": len(retained),
                        "crc_checked": True,
                        "tone_contract_checked": verify_tones,
                    }
    raise TimeoutError("timed out before a synchronized two-channel FFT update")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = capture_update(
            args.host,
            args.port,
            args.timeout,
            output=args.output,
            verify_tones=not args.no_verify_tones,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
