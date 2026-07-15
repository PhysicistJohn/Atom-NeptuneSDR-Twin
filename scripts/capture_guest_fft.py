#!/usr/bin/env python3
"""Receive and verify one two-channel NSFT update from ARM guest firmware."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPOSITORY / "src"
if str(SOURCE_TREE) not in sys.path:
    sys.path.insert(0, str(SOURCE_TREE))

from neptunesdr_twin.fft import PayloadEncoding, SpectrumPacket  # noqa: E402
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder  # noqa: E402


EXPECTED_TONE_BINS = {0: 65_536 * 5 // 64, 1: 65_536 * 13 // 64}
EXPECTED_TONE_DBFS = {0: -2.53, 1: -6.08}
TONE_DBFS_TOLERANCE = 0.10
MAX_PENDING_SEQUENCES = 4


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
        "timestamp_ns": packet.timestamp_ns,
        "config_epoch": packet.config_epoch,
        "peak_bin": bin_index,
        "peak_offset_hz": offset_hz,
        "peak_dbfs": value,
        "encoding": packet.encoding.name,
    }


def _validate_synchronized_update(packets: Sequence[SpectrumPacket]) -> None:
    if len(packets) != 2 or tuple(packet.channel for packet in packets) != (0, 1):
        raise RuntimeError("a synchronized update must contain channels 0 and 1 exactly once")
    first, second = packets
    if first.sequence != second.sequence:
        raise RuntimeError("synchronized channels have different sequence numbers")

    fields = (
        "version",
        "encoding",
        "fft_size",
        "sample_rate_hz",
        "center_frequency_hz",
        "timestamp_ns",
        "config_epoch",
        "bin_start",
        "bin_count",
        "dropped_frames",
        "overrun_events",
        "dropped_updates",
        "flags",
    )
    mismatched = [name for name in fields if getattr(first, name) != getattr(second, name)]
    if mismatched:
        raise RuntimeError(
            "synchronized channels disagree on update metadata: %s"
            % ", ".join(mismatched)
        )
    if first.encoding is not PayloadEncoding.UINT16_LOG_POWER:
        raise RuntimeError("guest spectrum encoding is not UINT16_LOG_POWER")


def capture_update(
    host: str,
    port: int,
    timeout: float,
    *,
    output: Optional[Path] = None,
    verify_tones: bool = True,
) -> Dict[str, object]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    decoder = SpectrumStreamDecoder()
    by_sequence: Dict[int, Dict[int, Tuple[SpectrumPacket, bytes]]] = {}
    socket_bytes_received = 0

    with _connect(host, port, deadline) as connection:
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(256 * 1024)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("FFT service closed before a complete update")
            socket_bytes_received += len(chunk)
            for packet, wire_packet in decoder.feed_with_wire(chunk):
                channels = by_sequence.setdefault(packet.sequence, {})
                if packet.channel in channels:
                    raise RuntimeError(
                        "guest repeated channel %d for sequence %d"
                        % (packet.channel, packet.sequence)
                    )
                channels[packet.channel] = (packet, wire_packet)
                if 0 in channels and 1 in channels:
                    selected_records = (channels[0], channels[1])
                    selected: Sequence[SpectrumPacket] = tuple(
                        record[0] for record in selected_records
                    )
                    _validate_synchronized_update(selected)
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
                    selected_wire = b"".join(record[1] for record in selected_records)
                    if output is not None:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(selected_wire)
                    return {
                        "status": "passed",
                        "transport": "guest-arm-nsft-v1-tcp",
                        "sequence": selected[0].sequence,
                        "timestamp_ns": selected[0].timestamp_ns,
                        "config_epoch": selected[0].config_epoch,
                        "sample_rate_hz": selected[0].sample_rate_hz,
                        "center_frequency_hz": selected[0].center_frequency_hz,
                        "channels": results,
                        "wire_bytes_received": len(selected_wire),
                        "socket_bytes_received": socket_bytes_received,
                        "crc_checked": True,
                        "tone_contract_checked": verify_tones,
                    }
                if len(by_sequence) > MAX_PENDING_SEQUENCES:
                    raise RuntimeError(
                        "too many incomplete spectrum sequences without a synchronized pair"
                    )
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
