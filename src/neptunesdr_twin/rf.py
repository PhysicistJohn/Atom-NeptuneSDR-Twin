"""Deterministic two-channel RF and sample-contact behavioral model.

This module models the boundary between the AD9361 RF configuration, its two
complex sample lanes, and bounded digital streaming contacts.  It is not an
electromagnetic simulation.  The intentionally small signal model provides
the behaviors firmware depends on: tuning and bandwidth selection, gain and
saturation, deterministic stimulus, TX/RX loopback, sample ordering, and
observable backpressure/loss.

The packed sample ABI is one simultaneous 2x2 frame per eight bytes::

    RX1-I, RX1-Q, RX2-I, RX2-Q

Each component is a little-endian signed 16-bit integer.  Sequence, timestamp,
and configuration-epoch metadata are contact metadata and are therefore not
present in the packed wire representation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from fractions import Fraction
import math
import struct
from typing import Callable, Deque, Dict, Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar

from .ad9361 import AD9361, ENSMState
from .errors import BufferOverrun, ContractViolation, TwinError


INT16_MIN = -32768
INT16_MAX = 32767
COMPONENTS_PER_FRAME = 4
BYTES_PER_FRAME = COMPONENTS_PER_FRAME * 2
_FRAME_STRUCT = struct.Struct("<hhhh")


@dataclass(frozen=True)
class IQ:
    """One complex integer sample in converter-count units."""

    i: int = 0
    q: int = 0

    def __post_init__(self) -> None:
        for name, value in (("i", self.i), ("q", self.q)):
            if not isinstance(value, int):
                raise TypeError("IQ %s component must be an integer" % name)
            if not INT16_MIN <= value <= INT16_MAX:
                raise ValueError("IQ %s component must fit signed 16 bits" % name)

    @property
    def complex(self) -> complex:
        return complex(self.i, self.q)

    def __complex__(self) -> complex:
        return self.complex

    @classmethod
    def zero(cls) -> "IQ":
        return cls(0, 0)


@dataclass(frozen=True)
class IQFrame:
    """Simultaneous RX1/RX2 (or TX1/TX2) complex sample frame.

    ``sample_index`` and ``config_epoch`` make discontinuity and mid-stream
    reconfiguration visible.  They deliberately do not alter :meth:`pack`.
    """

    channel0: IQ
    channel1: IQ
    sample_index: int = 0
    config_epoch: int = 0
    timestamp_ns: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.channel0, IQ) or not isinstance(self.channel1, IQ):
            raise TypeError("IQFrame channels must be IQ values")
        if not isinstance(self.sample_index, int):
            raise TypeError("sample_index must be an integer")
        if not isinstance(self.config_epoch, int):
            raise TypeError("config_epoch must be an integer")
        if self.timestamp_ns is not None and not isinstance(self.timestamp_ns, int):
            raise TypeError("timestamp_ns must be an integer or None")

    @property
    def ch0(self) -> IQ:
        return self.channel0

    @property
    def ch1(self) -> IQ:
        return self.channel1

    @property
    def rx1(self) -> IQ:
        return self.channel0

    @property
    def rx2(self) -> IQ:
        return self.channel1

    @property
    def channels(self) -> Tuple[IQ, IQ]:
        return (self.channel0, self.channel1)

    def pack(self) -> bytes:
        return _FRAME_STRUCT.pack(
            self.channel0.i,
            self.channel0.q,
            self.channel1.i,
            self.channel1.q,
        )

    to_bytes = pack

    @classmethod
    def zero(
        cls,
        sample_index: int = 0,
        config_epoch: int = 0,
        timestamp_ns: Optional[int] = None,
    ) -> "IQFrame":
        return cls(IQ.zero(), IQ.zero(), sample_index, config_epoch, timestamp_ns)

    @classmethod
    def unpack(
        cls,
        data: bytes,
        *,
        sample_index: int = 0,
        config_epoch: int = 0,
        timestamp_ns: Optional[int] = None,
    ) -> "IQFrame":
        if len(data) != BYTES_PER_FRAME:
            raise ValueError("an IQ frame is exactly %d bytes" % BYTES_PER_FRAME)
        i0, q0, i1, q1 = _FRAME_STRUCT.unpack(data)
        return cls(IQ(i0, q0), IQ(i1, q1), sample_index, config_epoch, timestamp_ns)

    from_bytes = unpack


def pack_iq_frames(frames: Iterable[IQFrame]) -> bytes:
    """Pack frames in time-major, channel-major, I/Q order."""

    payload = bytearray()
    for frame in frames:
        if not isinstance(frame, IQFrame):
            raise TypeError("pack_iq_frames accepts IQFrame values")
        payload.extend(frame.pack())
    return bytes(payload)


def unpack_iq_frames(
    payload: bytes,
    *,
    start_index: int = 0,
    config_epoch: int = 0,
    start_timestamp_ns: Optional[int] = None,
    sample_rate_hz: Optional[int] = None,
) -> Tuple[IQFrame, ...]:
    """Decode packed signed-IQ frames and reconstruct optional metadata.

    A timestamp can only be reconstructed if both ``start_timestamp_ns`` and
    ``sample_rate_hz`` are supplied.  Its integer nanoseconds are floored,
    matching :class:`RFModel`'s deterministic sample clock.
    """

    payload = bytes(payload)
    if len(payload) % BYTES_PER_FRAME:
        raise ValueError("IQ payload length must be a multiple of %d" % BYTES_PER_FRAME)
    if not isinstance(start_index, int):
        raise TypeError("start_index must be an integer")
    if sample_rate_hz is not None and sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if (start_timestamp_ns is None) != (sample_rate_hz is None):
        raise ValueError("timestamp reconstruction requires both start time and sample rate")

    result: List[IQFrame] = []
    elapsed = Fraction(0, 1)
    period = Fraction(1_000_000_000, sample_rate_hz) if sample_rate_hz else None
    for number, offset in enumerate(range(0, len(payload), BYTES_PER_FRAME)):
        timestamp = None
        if start_timestamp_ns is not None:
            timestamp = start_timestamp_ns + int(elapsed)
            elapsed += period  # type: ignore[operator]
        result.append(
            IQFrame.unpack(
                payload[offset : offset + BYTES_PER_FRAME],
                sample_index=start_index + number,
                config_epoch=config_epoch,
                timestamp_ns=timestamp,
            )
        )
    return tuple(result)


class OverrunPolicy(str, Enum):
    """Action when a producer exceeds FIFO capacity."""

    RAISE = "raise"
    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


class UnderrunPolicy(str, Enum):
    """Action when a consumer requests more items than are available."""

    RAISE = "raise"
    RETURN_AVAILABLE = "return_available"
    ZERO_FILL = "zero_fill"


class BufferUnderrun(TwinError):
    """A bounded streaming contact could not provide the requested frames."""


@dataclass(frozen=True)
class FIFOStats:
    capacity: int
    depth: int
    high_watermark: int
    pushed_frames: int
    popped_frames: int
    overrun_events: int
    overrun_frames: int
    underrun_events: int
    underrun_frames: int
    discarded_frames: int

    @property
    def lost_frames(self) -> int:
        return self.overrun_frames + self.discarded_frames


T = TypeVar("T")


class BoundedFIFO(Generic[T]):
    """Bounded FIFO whose every rejected, missing, or discarded item is counted."""

    def __init__(
        self,
        capacity: int,
        *,
        overrun_policy: OverrunPolicy = OverrunPolicy.RAISE,
        underrun_policy: UnderrunPolicy = UnderrunPolicy.RAISE,
        fill_factory: Optional[Callable[[], T]] = None,
    ) -> None:
        if int(capacity) <= 0:
            raise ValueError("FIFO capacity must be positive")
        self.capacity = int(capacity)
        self.overrun_policy = OverrunPolicy(overrun_policy)
        self.underrun_policy = UnderrunPolicy(underrun_policy)
        self.fill_factory = fill_factory
        self._items: Deque[T] = deque()
        self.high_watermark = 0
        self.pushed_frames = 0
        self.popped_frames = 0
        self.overrun_events = 0
        self.overrun_frames = 0
        self.underrun_events = 0
        self.underrun_frames = 0
        self.discarded_frames = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def free(self) -> int:
        return self.capacity - len(self._items)

    @property
    def overrun_count(self) -> int:
        return self.overrun_events

    @property
    def underrun_count(self) -> int:
        return self.underrun_events

    @property
    def stats(self) -> FIFOStats:
        return FIFOStats(
            capacity=self.capacity,
            depth=len(self._items),
            high_watermark=self.high_watermark,
            pushed_frames=self.pushed_frames,
            popped_frames=self.popped_frames,
            overrun_events=self.overrun_events,
            overrun_frames=self.overrun_frames,
            underrun_events=self.underrun_events,
            underrun_frames=self.underrun_frames,
            discarded_frames=self.discarded_frames,
        )

    def push(self, items: Iterable[T]) -> int:
        values = list(items)
        if not values:
            return 0
        overflow = max(0, len(self._items) + len(values) - self.capacity)
        if overflow:
            self.overrun_events += 1
            self.overrun_frames += overflow
            if self.overrun_policy == OverrunPolicy.RAISE:
                raise BufferOverrun(
                    "FIFO needs %d free slots but only %d are available"
                    % (len(values), self.free)
                )
            if self.overrun_policy == OverrunPolicy.DROP_NEWEST:
                values = values[: self.free]
            else:  # DROP_OLDEST preserves the newest capacity-sized suffix.
                for _ in range(min(overflow, len(self._items))):
                    self._items.popleft()
                if len(values) > self.capacity:
                    values = values[-self.capacity :]

        self._items.extend(values)
        accepted = len(values)
        self.pushed_frames += accepted
        self.high_watermark = max(self.high_watermark, len(self._items))
        return accepted

    put = push

    def pop(self, count: int) -> Tuple[T, ...]:
        count = int(count)
        if count < 0:
            raise ValueError("FIFO pop count must be non-negative")
        if count == 0:
            return ()
        available = min(count, len(self._items))
        missing = count - available
        if missing:
            self.underrun_events += 1
            self.underrun_frames += missing
            if self.underrun_policy == UnderrunPolicy.RAISE:
                raise BufferUnderrun(
                    "FIFO requested %d frames but only %d are available"
                    % (count, len(self._items))
                )
            if self.underrun_policy == UnderrunPolicy.ZERO_FILL and self.fill_factory is None:
                raise ContractViolation("ZERO_FILL FIFO requires a fill_factory")

        result = [self._items.popleft() for _ in range(available)]
        self.popped_frames += available
        if missing and self.underrun_policy == UnderrunPolicy.ZERO_FILL:
            result.extend(self.fill_factory() for _ in range(missing))  # type: ignore[misc]
        return tuple(result)

    get = pop

    def clear(self) -> int:
        """Explicitly discard queued items and return the recorded count."""

        count = len(self._items)
        self._items.clear()
        self.discarded_frames += count
        return count


@dataclass
class _Tone:
    source_id: int
    channel: int
    frequency_hz: float
    amplitude: float
    phase_rad: float
    enabled: bool = True


class _DeterministicNormal:
    """Small version-independent xorshift/Box-Muller normal generator."""

    _MASK = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        self.state = int(seed) & self._MASK
        if not self.state:
            self.state = 0x9E3779B97F4A7C15
        self._spare: Optional[float] = None

    def _uniform(self) -> float:
        value = self.state
        value ^= (value >> 12) & self._MASK
        value ^= (value << 25) & self._MASK
        value ^= (value >> 27) & self._MASK
        self.state = value & self._MASK
        value = (self.state * 2685821657736338717) & self._MASK
        return ((value >> 11) + 0.5) / float(1 << 53)

    def sample(self) -> float:
        if self._spare is not None:
            value = self._spare
            self._spare = None
            return value
        radius = math.sqrt(-2.0 * math.log(self._uniform()))
        angle = 2.0 * math.pi * self._uniform()
        self._spare = radius * math.sin(angle)
        return radius * math.cos(angle)


class RFModel:
    """Deterministic two-input/two-output RF sample model for an :class:`AD9361`.

    Tone amplitudes, noise RMS, TX samples, and output samples all use signed
    16-bit converter-count units.  Gain and coupling use voltage dB (20 log10).
    The RX bandwidth is modeled as an ideal complex-baseband passband; this is
    intentionally deterministic and makes tuning boundary behavior testable.
    """

    def __init__(
        self,
        radio: AD9361,
        *,
        fifo_capacity_frames: int = 4096,
        rx_overrun_policy: OverrunPolicy = OverrunPolicy.RAISE,
        rx_underrun_policy: UnderrunPolicy = UnderrunPolicy.RAISE,
        tx_overrun_policy: OverrunPolicy = OverrunPolicy.RAISE,
        tx_underrun_policy: UnderrunPolicy = UnderrunPolicy.ZERO_FILL,
        noise_seed: int = 1,
    ) -> None:
        if not isinstance(radio, AD9361):
            raise TypeError("RFModel requires an AD9361 configuration model")
        self.radio = radio
        self.trace = radio.trace
        self.rx_fifo: BoundedFIFO[IQFrame] = BoundedFIFO(
            fifo_capacity_frames,
            overrun_policy=rx_overrun_policy,
            underrun_policy=rx_underrun_policy,
            fill_factory=lambda: IQFrame.zero(-1, self.radio.config_epoch),
        )
        self.tx_fifo: BoundedFIFO[IQFrame] = BoundedFIFO(
            fifo_capacity_frames,
            overrun_policy=tx_overrun_policy,
            underrun_policy=tx_underrun_policy,
            fill_factory=lambda: IQFrame.zero(-1, self.radio.config_epoch),
        )
        self.sample_index = 0
        self.tx_input_index = 0
        self._sample_time_ns = Fraction(0, 1)
        self._observed_config_epoch: Optional[int] = None
        self._configuration_boundaries: List[Tuple[int, int]] = []
        self._tones: Dict[int, _Tone] = {}
        self._next_tone_id = 0
        self.noise_rms = [0.0, 0.0]
        self._noise_seed = int(noise_seed)
        self._noise = [
            _DeterministicNormal(self._noise_seed ^ 0x243F6A8885A308D3),
            _DeterministicNormal(self._noise_seed ^ 0x13198A2E03707344),
        ]
        self.loopback_enabled = False
        self.loopback_coupling_db = -30.0
        self.cross_coupling_db: Optional[float] = None
        self._coupling_overrides: Dict[Tuple[int, int], Optional[float]] = {}
        self._loopback_phase_rad = 0.0
        self.clipped_frames = 0
        self.clipped_components = [0, 0]

    @property
    def next_sample_index(self) -> int:
        return self.sample_index

    @property
    def configuration_boundaries(self) -> Tuple[Tuple[int, int], ...]:
        """Pairs of ``(first sample index, AD9361 config epoch)``."""

        return tuple(self._configuration_boundaries)

    def add_rx_tone(
        self,
        channel: int,
        frequency_hz: float,
        amplitude: float = 1000.0,
        phase_rad: float = 0.0,
    ) -> int:
        """Add a continuous RF tone at an absolute frequency and return its ID."""

        self._check_channel(channel)
        if not math.isfinite(float(frequency_hz)):
            raise ValueError("tone frequency must be finite")
        if float(amplitude) < 0.0 or not math.isfinite(float(amplitude)):
            raise ValueError("tone amplitude must be finite and non-negative")
        if not math.isfinite(float(phase_rad)):
            raise ValueError("tone phase must be finite")
        source_id = self._next_tone_id
        self._next_tone_id += 1
        self._tones[source_id] = _Tone(
            source_id,
            int(channel),
            float(frequency_hz),
            float(amplitude),
            float(phase_rad) % (2.0 * math.pi),
        )
        return source_id

    add_tone = add_rx_tone

    def add_baseband_tone(
        self,
        channel: int,
        offset_hz: float,
        amplitude: float = 1000.0,
        phase_rad: float = 0.0,
    ) -> int:
        """Add a fixed RF tone currently ``offset_hz`` from the RX LO."""

        return self.add_rx_tone(
            channel,
            self.radio.rx_lo_hz + float(offset_hz),
            amplitude,
            phase_rad,
        )

    def remove_tone(self, source_id: int) -> None:
        try:
            del self._tones[int(source_id)]
        except KeyError:
            raise KeyError("unknown RF tone source %s" % source_id)

    def clear_tones(self) -> None:
        self._tones.clear()

    def set_noise_rms(self, channel: int, rms_counts: float) -> None:
        self._check_channel(channel)
        rms_counts = float(rms_counts)
        if rms_counts < 0.0 or not math.isfinite(rms_counts):
            raise ValueError("noise RMS must be finite and non-negative")
        self.noise_rms[channel] = rms_counts

    def configure_loopback(
        self,
        coupling_db: float = -30.0,
        *,
        enabled: bool = True,
        cross_coupling_db: Optional[float] = None,
    ) -> None:
        """Configure diagonal TX1->RX1/TX2->RX2 and optional cross coupling."""

        self.loopback_coupling_db = self._check_db(coupling_db, "loopback coupling")
        if cross_coupling_db is not None:
            cross_coupling_db = self._check_db(cross_coupling_db, "cross coupling")
        self.cross_coupling_db = cross_coupling_db
        self.loopback_enabled = bool(enabled)

    set_loopback = configure_loopback

    def set_coupling(
        self, rx_channel: int, tx_channel: int, coupling_db: Optional[float]
    ) -> None:
        """Override one element of the RX-by-TX loopback coupling matrix.

        ``None`` disconnects that matrix element.  Calling this method also
        enables loopback, because an explicit contact has been configured.
        """

        self._check_channel(rx_channel)
        self._check_channel(tx_channel)
        if coupling_db is not None:
            coupling_db = self._check_db(coupling_db, "coupling")
        self._coupling_overrides[(rx_channel, tx_channel)] = coupling_db
        self.loopback_enabled = True

    @property
    def coupling_matrix_db(
        self,
    ) -> Tuple[Tuple[Optional[float], Optional[float]], Tuple[Optional[float], Optional[float]]]:
        result: List[List[Optional[float]]] = [[None, None], [None, None]]
        for rx_channel in range(2):
            for tx_channel in range(2):
                default = (
                    self.loopback_coupling_db
                    if rx_channel == tx_channel
                    else self.cross_coupling_db
                )
                result[rx_channel][tx_channel] = self._coupling_overrides.get(
                    (rx_channel, tx_channel), default
                )
        return (tuple(result[0]), tuple(result[1]))  # type: ignore[return-value]

    def write_tx_frames(self, frames: Iterable[IQFrame]) -> int:
        values = tuple(frames)
        accepted = self.tx_fifo.push(values)
        self._record(
            "tx_frames",
            "digital->rf",
            {"offered": len(values), "accepted": accepted, "fifo_depth": len(self.tx_fifo)},
        )
        return accepted

    push_tx_frames = write_tx_frames

    def write_tx_bytes(self, payload: bytes) -> int:
        frames = unpack_iq_frames(
            payload,
            start_index=self.tx_input_index,
            config_epoch=self.radio.config_epoch,
        )
        # The ingress sequence is consumed even when a selected FIFO policy
        # rejects it; any resulting loss is explicit in tx_fifo statistics.
        self.tx_input_index += len(frames)
        return self.write_tx_frames(frames)

    push_tx_bytes = write_tx_bytes

    def synthesize(self, frame_count: int) -> Tuple[IQFrame, ...]:
        """Return newly owned RX frames without placing them in ``rx_fifo``."""

        frame_count = self._check_count(frame_count)
        if not frame_count:
            return ()

        tx_frames: Sequence[IQFrame] = ()
        if self.loopback_enabled:
            tx_frames = self.tx_fifo.pop(frame_count)
            if len(tx_frames) < frame_count:
                # RETURN_AVAILABLE still produces a continuous RX stream.  The
                # underflow is already explicit in the FIFO counters.
                tx_frames = tuple(tx_frames) + tuple(
                    IQFrame.zero(-1, self.radio.config_epoch)
                    for _ in range(frame_count - len(tx_frames))
                )

        produced: List[IQFrame] = []
        first_index = self.sample_index
        for offset in range(frame_count):
            self._observe_epoch()
            tx_frame = tx_frames[offset] if tx_frames else None
            produced.append(self._synthesize_one(tx_frame))

        self._record(
            "synthesize",
            "rf->digital",
            {
                "first_sample_index": first_index,
                "frame_count": frame_count,
                "last_sample_index": self.sample_index - 1,
            },
        )
        return tuple(produced)

    capture = synthesize

    def produce(self, frame_count: int) -> Tuple[IQFrame, ...]:
        """Synthesize frames, enqueue them on RX, and return the offered frames."""

        frames = self.synthesize(frame_count)
        accepted = self.rx_fifo.push(frames)
        self._record(
            "rx_enqueue",
            "rf->digital",
            {"offered": len(frames), "accepted": accepted, "fifo_depth": len(self.rx_fifo)},
        )
        return frames

    generate = produce
    generate_rx_frames = produce

    def read_rx_frames(self, frame_count: int) -> Tuple[IQFrame, ...]:
        frames = self.rx_fifo.pop(self._check_count(frame_count))
        self._record(
            "rx_frames",
            "digital->consumer",
            {"requested": frame_count, "returned": len(frames), "fifo_depth": len(self.rx_fifo)},
        )
        return frames

    pop_rx_frames = read_rx_frames

    def read_rx_bytes(self, byte_count: int) -> bytes:
        byte_count = int(byte_count)
        if byte_count < 0 or byte_count % BYTES_PER_FRAME:
            raise ValueError("RX byte count must be a non-negative multiple of %d" % BYTES_PER_FRAME)
        return pack_iq_frames(self.read_rx_frames(byte_count // BYTES_PER_FRAME))

    pop_rx_bytes = read_rx_bytes

    def stream_rx_frames(self, frame_count: int) -> Tuple[IQFrame, ...]:
        """Produce enough samples to satisfy one exact streaming transfer."""

        frame_count = self._check_count(frame_count)
        needed = max(0, frame_count - len(self.rx_fifo))
        if needed:
            self.produce(needed)
        return self.read_rx_frames(frame_count)

    def stream_rx_bytes(self, byte_count: int) -> bytes:
        byte_count = int(byte_count)
        if byte_count < 0 or byte_count % BYTES_PER_FRAME:
            raise ValueError("RX byte count must be a non-negative multiple of %d" % BYTES_PER_FRAME)
        return pack_iq_frames(self.stream_rx_frames(byte_count // BYTES_PER_FRAME))

    def snapshot(self) -> Dict[str, object]:
        return {
            "sample_index": self.sample_index,
            "tx_input_index": self.tx_input_index,
            "sample_time_ns": int(self._sample_time_ns),
            "observed_config_epoch": self._observed_config_epoch,
            "configuration_boundaries": list(self._configuration_boundaries),
            "tone_count": len(self._tones),
            "noise_rms": list(self.noise_rms),
            "loopback_enabled": self.loopback_enabled,
            "coupling_matrix_db": [list(row) for row in self.coupling_matrix_db],
            "clipped_frames": self.clipped_frames,
            "clipped_components": list(self.clipped_components),
            "rx_fifo": asdict(self.rx_fifo.stats),
            "tx_fifo": asdict(self.tx_fifo.stats),
        }

    def _synthesize_one(self, tx_frame: Optional[IQFrame]) -> IQFrame:
        sample_rate = int(self.radio.sample_rate_hz)
        epoch = int(self.radio.config_epoch)
        timestamp_ns = int(self._sample_time_ns)
        rx_active = self.radio.state in (ENSMState.RX, ENSMState.FDD)
        tx_active = self.radio.state in (ENSMState.TX, ENSMState.FDD)
        accumulators = [0j, 0j]

        # Advancing every source even while muted or out of band preserves
        # phase when firmware enables a lane or retunes into the passband.
        half_bandwidth = min(float(self.radio.rx_bandwidth_hz), float(sample_rate)) / 2.0
        for source_id in sorted(self._tones):
            source = self._tones[source_id]
            offset_hz = source.frequency_hz - float(self.radio.rx_lo_hz)
            if (
                source.enabled
                and rx_active
                and self.radio.rx_channels[source.channel].enabled
                and abs(offset_hz) <= half_bandwidth
            ):
                accumulators[source.channel] += source.amplitude * complex(
                    math.cos(source.phase_rad), math.sin(source.phase_rad)
                )
            source.phase_rad = (
                source.phase_rad + 2.0 * math.pi * offset_hz / sample_rate
            ) % (2.0 * math.pi)

        lo_offset_hz = float(self.radio.tx_lo_hz - self.radio.rx_lo_hz)
        if self.loopback_enabled and tx_frame is not None:
            rotation = complex(
                math.cos(self._loopback_phase_rad), math.sin(self._loopback_phase_rad)
            )
            if rx_active and tx_active and abs(lo_offset_hz) <= half_bandwidth:
                matrix = self.coupling_matrix_db
                for rx_channel in range(2):
                    if not self.radio.rx_channels[rx_channel].enabled:
                        continue
                    for tx_channel in range(2):
                        coupling_db = matrix[rx_channel][tx_channel]
                        if coupling_db is None or not self.radio.tx_channels[tx_channel].enabled:
                            continue
                        attenuation_db = self.radio.tx_channels[tx_channel].attenuation_db
                        linear = self._db_to_linear(coupling_db - attenuation_db)
                        accumulators[rx_channel] += (
                            tx_frame.channels[tx_channel].complex * rotation * linear
                        )
            self._loopback_phase_rad = (
                self._loopback_phase_rad + 2.0 * math.pi * lo_offset_hz / sample_rate
            ) % (2.0 * math.pi)

        if rx_active:
            # Complex white-noise power passed by an ideal complex filter.
            noise_scale = math.sqrt(min(1.0, float(self.radio.rx_bandwidth_hz) / sample_rate))
            for channel in range(2):
                if not self.radio.rx_channels[channel].enabled or not self.noise_rms[channel]:
                    continue
                sigma = self.noise_rms[channel] * noise_scale
                accumulators[channel] += complex(
                    self._noise[channel].sample() * sigma,
                    self._noise[channel].sample() * sigma,
                )

        outputs: List[IQ] = []
        frame_clipped = False
        for channel in range(2):
            if not rx_active or not self.radio.rx_channels[channel].enabled:
                outputs.append(IQ.zero())
                continue
            gain = self._db_to_linear(self.radio.rx_channels[channel].gain_db)
            i_value, i_clipped = self._quantize(accumulators[channel].real * gain)
            q_value, q_clipped = self._quantize(accumulators[channel].imag * gain)
            if i_clipped or q_clipped:
                self.clipped_components[channel] += int(i_clipped) + int(q_clipped)
                frame_clipped = True
            outputs.append(IQ(i_value, q_value))
        if frame_clipped:
            self.clipped_frames += 1

        frame = IQFrame(
            outputs[0],
            outputs[1],
            sample_index=self.sample_index,
            config_epoch=epoch,
            timestamp_ns=timestamp_ns,
        )
        self.sample_index += 1
        self._sample_time_ns += Fraction(1_000_000_000, sample_rate)
        return frame

    def _observe_epoch(self) -> None:
        epoch = int(self.radio.config_epoch)
        if epoch != self._observed_config_epoch:
            self._observed_config_epoch = epoch
            self._configuration_boundaries.append((self.sample_index, epoch))

    def _record(self, event: str, direction: str, payload: Dict[str, object]) -> None:
        self.trace.append(
            logical_ns=self.radio.clock.now_ns,
            contact="ad9361.iq",
            direction=direction,
            event=event,
            payload=payload,
            config_epoch=self.radio.config_epoch,
        )

    @staticmethod
    def _check_channel(channel: int) -> None:
        if channel not in (0, 1):
            raise IndexError("RF channel must be 0 or 1")

    @staticmethod
    def _check_count(count: int) -> int:
        count = int(count)
        if count < 0:
            raise ValueError("frame count must be non-negative")
        return count

    @staticmethod
    def _check_db(value: float, label: str) -> float:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("%s must be finite" % label)
        return value

    @staticmethod
    def _db_to_linear(db: float) -> float:
        return 10.0 ** (float(db) / 20.0)

    @staticmethod
    def _quantize(value: float) -> Tuple[int, bool]:
        if value >= 0.0:
            rounded = int(math.floor(value + 0.5))
        else:
            rounded = int(math.ceil(value - 0.5))
        if rounded > INT16_MAX:
            return INT16_MAX, True
        if rounded < INT16_MIN:
            return INT16_MIN, True
        return rounded, False


__all__ = [
    "BYTES_PER_FRAME",
    "INT16_MAX",
    "INT16_MIN",
    "BoundedFIFO",
    "BufferOverrun",
    "BufferUnderrun",
    "FIFOStats",
    "IQ",
    "IQFrame",
    "OverrunPolicy",
    "RFModel",
    "UnderrunPolicy",
    "pack_iq_frames",
    "unpack_iq_frames",
]
