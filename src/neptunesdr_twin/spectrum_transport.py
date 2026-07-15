"""Host transport for the self-framing NSFT spectrum packets.

The same TCP byte stream can cross physical Gigabit Ethernet or the USB RNDIS
network function.  Full 65,536-bin float32 packets exceed a UDP datagram, so
the twin deliberately exposes a reliable stream instead of silently
fragmenting spectra at an undocumented boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
import struct
import threading
from typing import Iterable, List, Mapping, Optional, Tuple

from .fft import (
    MAX_FFT_SIZE,
    PACKET_CRC_BYTES,
    PACKET_HEADER_BYTES,
    PACKET_MAGIC,
    PayloadEncoding,
    SpectrumPacket,
    unpack_spectrum_packet,
)


MAX_SPECTRUM_PACKET_BYTES = (
    PACKET_HEADER_BYTES
    + MAX_FFT_SIZE * PayloadEncoding.FLOAT32_DBFS.bytes_per_bin
    + PACKET_CRC_BYTES
)


class SpectrumStreamError(ValueError):
    pass


class SpectrumStreamDecoder:
    """Incrementally split a TCP stream into checked :class:`SpectrumPacket`s."""

    def __init__(self, maximum_packet_bytes: int = MAX_SPECTRUM_PACKET_BYTES) -> None:
        if type(maximum_packet_bytes) is not int or maximum_packet_bytes <= 0:
            raise ValueError("maximum_packet_bytes must be a positive integer")
        self.maximum_packet_bytes = maximum_packet_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> Tuple[SpectrumPacket, ...]:
        self._buffer.extend(bytes(data))
        packets: List[SpectrumPacket] = []
        while len(self._buffer) >= PACKET_HEADER_BYTES:
            if self._buffer[:4] != PACKET_MAGIC:
                raise SpectrumStreamError("spectrum stream lost NSFT framing")
            payload_bytes = struct.unpack(">I", self._buffer[PACKET_HEADER_BYTES - 4 : PACKET_HEADER_BYTES])[0]
            packet_bytes = PACKET_HEADER_BYTES + payload_bytes + PACKET_CRC_BYTES
            if packet_bytes > self.maximum_packet_bytes:
                raise SpectrumStreamError("declared spectrum packet exceeds the stream limit")
            if len(self._buffer) < packet_bytes:
                break
            raw = bytes(self._buffer[:packet_bytes])
            del self._buffer[:packet_bytes]
            packets.append(unpack_spectrum_packet(raw))
        return tuple(packets)


@dataclass(frozen=True)
class PublisherCounters:
    accepted_clients: int = 0
    disconnected_clients: int = 0
    published_updates: int = 0
    published_packets: int = 0
    published_bytes: int = 0


class SpectrumTCPPublisher:
    """Small deterministic TCP publisher for already packetized FFT results."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        backlog: int = 4,
        send_timeout: float = 1.0,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be in [0, 65535]")
        if backlog <= 0 or send_timeout <= 0:
            raise ValueError("backlog and send_timeout must be positive")
        self.host = host
        self.port = port
        self.backlog = backlog
        self.send_timeout = send_timeout
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: List[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._counters = PublisherCounters()

    @property
    def address(self) -> Tuple[str, int]:
        if self._listener is None:
            raise RuntimeError("spectrum publisher is not running")
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def counters(self) -> PublisherCounters:
        with self._lock:
            return self._counters

    def start(self) -> "SpectrumTCPPublisher":
        if self._listener is not None:
            raise RuntimeError("spectrum publisher is already running")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(self.backlog)
        listener.settimeout(0.1)
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="neptune-spectrum-publisher",
            daemon=True,
        )
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(self.send_timeout)
            with self._lock:
                self._clients.append(client)
                current = self._counters
                self._counters = PublisherCounters(
                    accepted_clients=current.accepted_clients + 1,
                    disconnected_clients=current.disconnected_clients,
                    published_updates=current.published_updates,
                    published_packets=current.published_packets,
                    published_bytes=current.published_bytes,
                )

    def publish(self, packets: Iterable[SpectrumPacket]) -> int:
        selected = tuple(packets)
        if not selected:
            return 0
        wire = b"".join(packet.pack() for packet in selected)
        with self._lock:
            clients = tuple(self._clients)
        delivered = 0
        failed: List[socket.socket] = []
        for client in clients:
            try:
                client.sendall(wire)
                delivered += 1
            except (OSError, socket.timeout):
                failed.append(client)
        with self._lock:
            for client in failed:
                if client in self._clients:
                    self._clients.remove(client)
                    try:
                        client.close()
                    except OSError:
                        pass
            current = self._counters
            self._counters = PublisherCounters(
                accepted_clients=current.accepted_clients,
                disconnected_clients=current.disconnected_clients + len(failed),
                published_updates=current.published_updates + 1,
                published_packets=current.published_packets + len(selected),
                published_bytes=current.published_bytes + len(wire),
            )
        return delivered

    def snapshot(self) -> Mapping[str, object]:
        counters = self.counters
        return {
            "running": self._listener is not None,
            "address": list(self.address) if self._listener is not None else None,
            "clients": self.client_count,
            "accepted_clients": counters.accepted_clients,
            "disconnected_clients": counters.disconnected_clients,
            "published_updates": counters.published_updates,
            "published_packets": counters.published_packets,
            "published_bytes": counters.published_bytes,
        }

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            clients = tuple(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def __enter__(self) -> "SpectrumTCPPublisher":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


__all__ = [
    "MAX_SPECTRUM_PACKET_BYTES",
    "PublisherCounters",
    "SpectrumStreamDecoder",
    "SpectrumStreamError",
    "SpectrumTCPPublisher",
]
