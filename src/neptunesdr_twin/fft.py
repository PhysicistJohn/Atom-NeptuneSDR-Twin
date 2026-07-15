"""Deterministic FFT reference model and spectrum-result wire contract.

The P210 cannot continuously carry two 61.44 MSPS, 16-bit complex streams to
the host under the vendor's 48 MB/s payload claim.  This module models the
intended alternative: consume IQ in programmable logic, average FFT powers,
and send only selected spectrum bins.

The FFT is deliberately a small, dependency-free radix-2 implementation.  It
is a numerical reference for firmware/RTL tests, not a claim about a specific
vendor FFT IP configuration or its post-route timing/resource use.
"""

from __future__ import annotations

import binascii
import math
import struct
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum, IntFlag
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple, Union


MIN_FFT_SIZE = 256
MAX_FFT_SIZE = 65_536
PACKET_MAGIC = b"NSFT"
PACKET_VERSION = 1
VENDOR_HOST_PAYLOAD_BYTES_PER_SECOND = 48_000_000
UINT16_LOG_MIN_DBFS = -200.0
UINT16_LOG_STEP_DB = 0.01


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def _coerce_enum(value: object, enum_type: object, label: str) -> object:
    try:
        return enum_type(value)  # type: ignore[operator]
    except (TypeError, ValueError):
        if isinstance(value, str):
            normalized = value.strip().lower().replace("_", "-")
            for member in enum_type:  # type: ignore[union-attr]
                if member.value == normalized or member.name.lower().replace("_", "-") == normalized:
                    return member
        raise ValueError("unsupported %s: %r" % (label, value))


def _check_uint(value: int, bits: int, label: str) -> None:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError("%s must fit an unsigned %d-bit integer" % (label, bits))


class FFTWindow(str, Enum):
    RECTANGULAR = "rectangular"
    HANN = "hann"
    BLACKMAN = "blackman"


# A concise alias is convenient at call sites and retains an explicit public
# name for documentation.
Window = FFTWindow


class PayloadEncoding(IntEnum):
    """Spectrum payload representation.

    FLOAT32_DBFS stores IEEE-754 values directly.  UINT16_LOG_POWER maps code
    zero to -200 dBFS and uses 0.01 dB/code, saturating at both ends.  All wire
    fields and payload elements use network byte order.
    """

    FLOAT32_DBFS = 1
    UINT16_LOG_POWER = 2

    @property
    def bytes_per_bin(self) -> int:
        if self is PayloadEncoding.FLOAT32_DBFS:
            return 4
        return 2


SpectrumEncoding = PayloadEncoding


class PacketFlag(IntFlag):
    NONE = 0
    DROPPED_FRAMES = 1 << 0
    INPUT_OVERRUN = 1 << 1
    DROPPED_UPDATES = 1 << 2


class BackpressureMode(str, Enum):
    NONE = "none"
    READY_VALID = "ready-valid"


class OverflowPolicy(str, Enum):
    DROP_FRAME_AND_REPORT = "drop-frame-and-report"
    STOP_AND_REPORT = "stop-and-report"


class ProcessingStatus(str, Enum):
    ACCUMULATING = "accumulating"
    EMITTED = "emitted"
    BACKPRESSURED = "backpressured"
    DROPPED = "dropped"
    OVERRUN = "overrun"


@dataclass(frozen=True)
class FFTConfig:
    """Configuration shared by the FFT, averager, and packetizer.

    IQ values are normalized complex samples: a complex sinusoid with magnitude
    ``full_scale`` is 0 dBFS.  Hann and Blackman are periodic (FFT-analysis)
    windows.  Coherent-gain normalization makes an exactly bin-centred tone
    retain its amplitude through any supported window.
    """

    fft_size: int = 1024
    channels: int = 2
    window: FFTWindow = FFTWindow.HANN
    coherent_gain_normalization: bool = True
    fftshift: bool = True
    averages: int = 1
    update_rate_hz: float = 20.0
    bin_start: int = 0
    bin_count: Optional[int] = None
    sample_rate_hz: int = 61_440_000
    center_frequency_hz: int = 2_400_000_000
    config_epoch: int = 0
    payload_encoding: PayloadEncoding = PayloadEncoding.UINT16_LOG_POWER
    full_scale: float = 1.0
    dbfs_floor: float = UINT16_LOG_MIN_DBFS

    def __post_init__(self) -> None:
        if type(self.fft_size) is not int or not (
            MIN_FFT_SIZE <= self.fft_size <= MAX_FFT_SIZE
        ):
            raise ValueError("fft_size must be in [256, 65536]")
        if not _is_power_of_two(self.fft_size):
            raise ValueError("fft_size must be a power of two")
        if self.channels not in (1, 2):
            raise ValueError("channels must be one or two")
        object.__setattr__(self, "window", _coerce_enum(self.window, FFTWindow, "window"))
        object.__setattr__(
            self,
            "payload_encoding",
            _coerce_enum(self.payload_encoding, PayloadEncoding, "payload encoding"),
        )
        if type(self.averages) is not int or self.averages <= 0:
            raise ValueError("averages must be a positive integer")
        if not isinstance(self.update_rate_hz, (int, float)) or not math.isfinite(
            self.update_rate_hz
        ) or self.update_rate_hz <= 0:
            raise ValueError("update_rate_hz must be finite and positive")
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be a positive integer")
        _check_uint(self.center_frequency_hz, 64, "center_frequency_hz")
        _check_uint(self.config_epoch, 32, "config_epoch")
        if type(self.bin_start) is not int or not 0 <= self.bin_start < self.fft_size:
            raise ValueError("bin_start must address an FFT bin")
        count = self.fft_size - self.bin_start if self.bin_count is None else self.bin_count
        if type(count) is not int or count <= 0:
            raise ValueError("bin_count must be a positive integer")
        if self.bin_start + count > self.fft_size:
            raise ValueError("selected bin range exceeds fft_size")
        object.__setattr__(self, "bin_count", count)
        if not isinstance(self.full_scale, (int, float)) or not math.isfinite(
            self.full_scale
        ) or self.full_scale <= 0:
            raise ValueError("full_scale must be finite and positive")
        if not isinstance(self.dbfs_floor, (int, float)) or not math.isfinite(
            self.dbfs_floor
        ):
            raise ValueError("dbfs_floor must be finite")

    @property
    def input_frames_per_second(self) -> float:
        return self.sample_rate_hz / self.fft_size

    @property
    def frames_per_update(self) -> int:
        """Block-average depth after applying the requested rate ceiling."""

        rate_limited = int(math.ceil(self.input_frames_per_second / self.update_rate_hz))
        return max(self.averages, rate_limited, 1)

    @property
    def effective_update_rate_hz(self) -> float:
        return self.input_frames_per_second / self.frames_per_update

    @property
    def accumulation_frames(self) -> int:
        return self.frames_per_update


def window_coefficients(size: int, window: Union[FFTWindow, str]) -> Tuple[float, ...]:
    """Return a periodic FFT-analysis window of ``size`` samples."""

    if type(size) is not int or size <= 0:
        raise ValueError("window size must be a positive integer")
    kind = _coerce_enum(window, FFTWindow, "window")
    if kind is FFTWindow.RECTANGULAR:
        return (1.0,) * size
    if kind is FFTWindow.HANN:
        return tuple(0.5 - 0.5 * math.cos(2.0 * math.pi * n / size) for n in range(size))
    return tuple(
        0.42
        - 0.5 * math.cos(2.0 * math.pi * n / size)
        + 0.08 * math.cos(4.0 * math.pi * n / size)
        for n in range(size)
    )


def coherent_gain(coefficients: Sequence[float]) -> float:
    if not coefficients:
        raise ValueError("coherent gain needs at least one coefficient")
    return math.fsum(coefficients) / len(coefficients)


def radix2_fft(samples: Sequence[complex]) -> Tuple[complex, ...]:
    """Unnormalised forward DFT using iterative radix-2 Cooley-Tukey.

    Unlike :class:`FFTConfig`, this primitive permits tiny powers of two so it
    is useful for fast, hand-checkable unit and RTL-vector tests.
    """

    size = len(samples)
    if not _is_power_of_two(size):
        raise ValueError("radix2_fft input length must be a positive power of two")
    values: List[complex] = []
    for sample in samples:
        value = complex(sample)
        if not (math.isfinite(value.real) and math.isfinite(value.imag)):
            raise ValueError("FFT samples must be finite")
        values.append(value)

    # In-place bit-reversal permutation.
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]

    span = 2
    while span <= size:
        angle = -2.0 * math.pi / span
        twiddle_step = complex(math.cos(angle), math.sin(angle))
        half = span // 2
        for base in range(0, size, span):
            twiddle = 1.0 + 0.0j
            for offset in range(half):
                even = values[base + offset]
                odd = values[base + offset + half] * twiddle
                values[base + offset] = even + odd
                values[base + offset + half] = even - odd
                twiddle *= twiddle_step
        span <<= 1
    return tuple(values)


reference_fft = radix2_fft


def fftshift(values: Sequence[complex]) -> Tuple[complex, ...]:
    if len(values) % 2:
        raise ValueError("fftshift requires an even number of bins")
    middle = len(values) // 2
    return tuple(values[middle:]) + tuple(values[:middle])


def windowed_fft(
    samples: Sequence[complex],
    window: Union[FFTWindow, str] = FFTWindow.RECTANGULAR,
    coherent_gain_normalization: bool = True,
    shifted: bool = False,
) -> Tuple[complex, ...]:
    """FFT with conventional 1/N scaling and optional coherent-gain repair."""

    coefficients = window_coefficients(len(samples), window)
    transformed = radix2_fft(
        tuple(complex(sample) * coefficient for sample, coefficient in zip(samples, coefficients))
    )
    denominator = float(len(samples))
    if coherent_gain_normalization:
        denominator *= coherent_gain(coefficients)
    if denominator == 0.0:
        raise ValueError("window has zero coherent gain")
    normalized = tuple(value / denominator for value in transformed)
    return fftshift(normalized) if shifted else normalized


def power_spectrum_dbfs(samples: Sequence[complex], config: FFTConfig) -> Tuple[float, ...]:
    """Calculate selected, normalized power bins for one IQ channel."""

    if len(samples) != config.fft_size:
        raise ValueError("IQ frame length does not match fft_size")
    transformed = windowed_fft(
        samples,
        window=config.window,
        coherent_gain_normalization=config.coherent_gain_normalization,
        shifted=config.fftshift,
    )
    start = config.bin_start
    stop = start + int(config.bin_count)
    full_scale_power = float(config.full_scale) ** 2
    floor_power = 10.0 ** (float(config.dbfs_floor) / 10.0)
    result = []
    for value in transformed[start:stop]:
        power = (value.real * value.real + value.imag * value.imag) / full_scale_power
        result.append(10.0 * math.log10(max(power, floor_power)))
    return tuple(result)


calculate_power_spectrum = power_spectrum_dbfs


class PowerAccumulator:
    """Fixed-depth linear-power block averager.

    Inputs and outputs are linear powers.  Averaging dB values would be a
    different and generally undesirable operation.
    """

    def __init__(self, bin_count: int, frames: int = 1) -> None:
        if type(bin_count) is not int or bin_count <= 0:
            raise ValueError("bin_count must be positive")
        if type(frames) is not int or frames <= 0:
            raise ValueError("frames must be positive")
        self.bin_count = bin_count
        self.frames = frames
        self._sums = [0.0] * bin_count
        self.count = 0

    def reset(self) -> None:
        self._sums = [0.0] * self.bin_count
        self.count = 0

    def add(self, powers: Sequence[float]) -> Optional[Tuple[float, ...]]:
        if len(powers) != self.bin_count:
            raise ValueError("power vector has the wrong bin count")
        for index, power in enumerate(powers):
            if not isinstance(power, (int, float)) or not math.isfinite(power) or power < 0:
                raise ValueError("linear powers must be finite and non-negative")
            self._sums[index] += float(power)
        self.count += 1
        if self.count < self.frames:
            return None
        averaged = tuple(total / self.count for total in self._sums)
        self.reset()
        return averaged


@dataclass(frozen=True)
class PLResourceContract:
    """Static bounds reserved for an RTL implementation.

    DSP and BRAM budgets are optional because actual counts must come from the
    selected FFT IP configuration and synthesis report; leaving them absent is
    explicit rather than inventing a utilization claim.
    """

    max_fft_size: int = MAX_FFT_SIZE
    max_channels: int = 2
    fft_engines: int = 2
    sample_component_bits: int = 16
    twiddle_bits: int = 18
    dsp_slices_budget: Optional[int] = None
    bram_36k_budget: Optional[int] = None

    def __post_init__(self) -> None:
        if not _is_power_of_two(self.max_fft_size) or self.max_fft_size > MAX_FFT_SIZE:
            raise ValueError("max_fft_size must be a supported power of two")
        if self.max_channels not in (1, 2):
            raise ValueError("max_channels must be one or two")
        for label in ("fft_engines", "sample_component_bits", "twiddle_bits"):
            if type(getattr(self, label)) is not int or getattr(self, label) <= 0:
                raise ValueError("%s must be positive" % label)
        for label in ("dsp_slices_budget", "bram_36k_budget"):
            value = getattr(self, label)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("%s must be non-negative or absent" % label)

    def supports(self, config: FFTConfig) -> bool:
        return config.fft_size <= self.max_fft_size and config.channels <= self.max_channels


@dataclass(frozen=True)
class PLThroughputAssessment:
    fits: bool
    required_complex_samples_per_second: int
    capacity_complex_samples_per_second: int
    utilization: float
    resource_fits: bool
    reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "fits": self.fits,
            "required_complex_samples_per_second": self.required_complex_samples_per_second,
            "capacity_complex_samples_per_second": self.capacity_complex_samples_per_second,
            "utilization": self.utilization,
            "resource_fits": self.resource_fits,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PLThroughputContract:
    """Assume/guarantee boundary for IQ ingress and FFT result egress.

    ``lanes`` is the total number of complex samples accepted per stream clock,
    across all enabled channels.  The AD9361-side source normally cannot be
    backpressured, while an AXI result sink can deassert READY.  A FIFO can hold
    ``result_fifo_updates`` complete multi-channel spectrum updates before the
    declared overflow policy applies.
    """

    stream_clock_hz: int
    lanes: int
    input_sample_rate_hz: int
    channels: int = 2
    input_backpressure: BackpressureMode = BackpressureMode.NONE
    output_backpressure: BackpressureMode = BackpressureMode.READY_VALID
    overflow_policy: OverflowPolicy = OverflowPolicy.DROP_FRAME_AND_REPORT
    result_fifo_updates: int = 1
    resources: PLResourceContract = field(default_factory=PLResourceContract)

    def __post_init__(self) -> None:
        for label in ("stream_clock_hz", "lanes", "input_sample_rate_hz"):
            if type(getattr(self, label)) is not int or getattr(self, label) <= 0:
                raise ValueError("%s must be a positive integer" % label)
        if self.channels not in (1, 2):
            raise ValueError("channels must be one or two")
        if type(self.result_fifo_updates) is not int or self.result_fifo_updates < 0:
            raise ValueError("result_fifo_updates must be non-negative")
        object.__setattr__(
            self,
            "input_backpressure",
            _coerce_enum(self.input_backpressure, BackpressureMode, "input backpressure"),
        )
        object.__setattr__(
            self,
            "output_backpressure",
            _coerce_enum(self.output_backpressure, BackpressureMode, "output backpressure"),
        )
        object.__setattr__(
            self,
            "overflow_policy",
            _coerce_enum(self.overflow_policy, OverflowPolicy, "overflow policy"),
        )

    @property
    def required_complex_samples_per_second(self) -> int:
        return self.input_sample_rate_hz * self.channels

    @property
    def capacity_complex_samples_per_second(self) -> int:
        return self.stream_clock_hz * self.lanes

    @property
    def utilization(self) -> float:
        return self.required_complex_samples_per_second / self.capacity_complex_samples_per_second

    @property
    def input_can_backpressure(self) -> bool:
        return self.input_backpressure is not BackpressureMode.NONE

    @property
    def output_can_backpressure(self) -> bool:
        return self.output_backpressure is not BackpressureMode.NONE

    def assess(self, config: Optional[FFTConfig] = None) -> PLThroughputAssessment:
        reasons = []
        throughput_fits = (
            self.required_complex_samples_per_second <= self.capacity_complex_samples_per_second
        )
        if not throughput_fits:
            reasons.append("aggregate IQ input exceeds stream clock times lanes")
        resource_fits = True
        if config is not None:
            if config.channels != self.channels:
                resource_fits = False
                reasons.append("FFT configuration channel count differs from PL contract")
            if config.sample_rate_hz != self.input_sample_rate_hz:
                resource_fits = False
                reasons.append("FFT configuration sample rate differs from PL contract")
            if not self.resources.supports(config):
                resource_fits = False
                reasons.append("FFT size or channel count exceeds reserved resource bounds")
        return PLThroughputAssessment(
            fits=throughput_fits and resource_fits,
            required_complex_samples_per_second=self.required_complex_samples_per_second,
            capacity_complex_samples_per_second=self.capacity_complex_samples_per_second,
            utilization=self.utilization,
            resource_fits=resource_fits,
            reasons=tuple(reasons),
        )


# Fixed header, payload, then a CRC32 over header+payload.  See SpectrumPacket.
_PACKET_HEADER = struct.Struct(">4sBBBBQIIQQIIIIIII")
_PACKET_CRC = struct.Struct(">I")
PACKET_HEADER_BYTES = _PACKET_HEADER.size
PACKET_CRC_BYTES = _PACKET_CRC.size
PACKET_OVERHEAD_BYTES = PACKET_HEADER_BYTES + PACKET_CRC_BYTES


class PacketError(ValueError):
    pass


class PacketCRCError(PacketError):
    pass


def _encode_payload(values: Sequence[float], encoding: PayloadEncoding) -> bytes:
    if encoding is PayloadEncoding.FLOAT32_DBFS:
        cleaned = []
        for value in values:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("float32 dBFS payload values must be finite")
            cleaned.append(number)
        try:
            return struct.pack(">%df" % len(cleaned), *cleaned)
        except (OverflowError, struct.error) as error:
            raise ValueError("dBFS value cannot be represented as float32") from error

    codes = []
    maximum = (1 << 16) - 1
    for value in values:
        number = float(value)
        if math.isnan(number) or number <= UINT16_LOG_MIN_DBFS:
            code = 0
        elif math.isinf(number) or number >= UINT16_LOG_MIN_DBFS + maximum * UINT16_LOG_STEP_DB:
            code = maximum
        else:
            scaled = (number - UINT16_LOG_MIN_DBFS) / UINT16_LOG_STEP_DB
            code = int(math.floor(scaled + 0.5))
        codes.append(code)
    return struct.pack(">%dH" % len(codes), *codes)


def _decode_payload(payload: bytes, encoding: PayloadEncoding) -> Tuple[float, ...]:
    if not payload:
        return ()
    count = len(payload) // encoding.bytes_per_bin
    if encoding is PayloadEncoding.FLOAT32_DBFS:
        return tuple(float(value) for value in struct.unpack(">%df" % count, payload))
    codes = struct.unpack(">%dH" % count, payload)
    return tuple(UINT16_LOG_MIN_DBFS + code * UINT16_LOG_STEP_DB for code in codes)


@dataclass(frozen=True)
class SpectrumPacket:
    sequence: int
    channel: int
    fft_size: int
    sample_rate_hz: int
    center_frequency_hz: int
    timestamp_ns: int
    config_epoch: int
    bin_start: int
    values_dbfs: Tuple[float, ...]
    encoding: PayloadEncoding = PayloadEncoding.UINT16_LOG_POWER
    dropped_frames: int = 0
    overrun_events: int = 0
    dropped_updates: int = 0
    flags: PacketFlag = PacketFlag.NONE
    version: int = PACKET_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "values_dbfs", tuple(float(v) for v in self.values_dbfs))
        object.__setattr__(
            self, "encoding", _coerce_enum(self.encoding, PayloadEncoding, "payload encoding")
        )
        object.__setattr__(self, "flags", PacketFlag(self.flags))
        _check_uint(self.version, 8, "version")
        if self.version != PACKET_VERSION:
            raise ValueError("only packet version %d can be constructed" % PACKET_VERSION)
        _check_uint(self.sequence, 64, "sequence")
        _check_uint(self.channel, 8, "channel")
        if self.channel not in (0, 1):
            raise ValueError("channel must be zero or one")
        _check_uint(self.fft_size, 32, "fft_size")
        if not (MIN_FFT_SIZE <= self.fft_size <= MAX_FFT_SIZE) or not _is_power_of_two(
            self.fft_size
        ):
            raise ValueError("packet fft_size must be a supported power of two")
        _check_uint(self.sample_rate_hz, 32, "sample_rate_hz")
        if self.sample_rate_hz == 0:
            raise ValueError("sample_rate_hz must be positive")
        _check_uint(self.center_frequency_hz, 64, "center_frequency_hz")
        _check_uint(self.timestamp_ns, 64, "timestamp_ns")
        _check_uint(self.config_epoch, 32, "config_epoch")
        _check_uint(self.bin_start, 32, "bin_start")
        if not self.values_dbfs:
            raise ValueError("a spectrum packet must contain at least one bin")
        if self.bin_start + self.bin_count > self.fft_size:
            raise ValueError("packet bin range exceeds fft_size")
        for label in ("dropped_frames", "overrun_events", "dropped_updates"):
            _check_uint(getattr(self, label), 32, label)

    @property
    def bin_count(self) -> int:
        return len(self.values_dbfs)

    @property
    def magic(self) -> bytes:
        return PACKET_MAGIC

    @property
    def payload(self) -> bytes:
        return _encode_payload(self.values_dbfs, self.encoding)

    def pack(self) -> bytes:
        return pack_spectrum_packet(self)

    @classmethod
    def unpack(cls, data: Union[bytes, bytearray, memoryview]) -> "SpectrumPacket":
        return unpack_spectrum_packet(data)


def pack_spectrum_packet(packet: SpectrumPacket) -> bytes:
    payload = packet.payload
    flags = packet.flags
    if packet.dropped_frames:
        flags |= PacketFlag.DROPPED_FRAMES
    if packet.overrun_events:
        flags |= PacketFlag.INPUT_OVERRUN
    if packet.dropped_updates:
        flags |= PacketFlag.DROPPED_UPDATES
    header = _PACKET_HEADER.pack(
        PACKET_MAGIC,
        packet.version,
        int(packet.encoding),
        packet.channel,
        int(flags),
        packet.sequence,
        packet.fft_size,
        packet.sample_rate_hz,
        packet.center_frequency_hz,
        packet.timestamp_ns,
        packet.config_epoch,
        packet.bin_start,
        packet.bin_count,
        packet.dropped_frames,
        packet.overrun_events,
        packet.dropped_updates,
        len(payload),
    )
    body = header + payload
    checksum = binascii.crc32(body) & 0xFFFFFFFF
    return body + _PACKET_CRC.pack(checksum)


pack_packet = pack_spectrum_packet


def unpack_spectrum_packet(data: Union[bytes, bytearray, memoryview]) -> SpectrumPacket:
    raw = bytes(data)
    minimum = PACKET_OVERHEAD_BYTES
    if len(raw) < minimum:
        raise PacketError("spectrum packet is truncated")
    unpacked = _PACKET_HEADER.unpack(raw[:PACKET_HEADER_BYTES])
    (
        magic,
        version,
        encoding_code,
        channel,
        flags,
        sequence,
        fft_size,
        sample_rate_hz,
        center_frequency_hz,
        timestamp_ns,
        config_epoch,
        bin_start,
        bin_count,
        dropped_frames,
        overrun_events,
        dropped_updates,
        payload_length,
    ) = unpacked
    if magic != PACKET_MAGIC:
        raise PacketError("invalid spectrum packet magic")
    if version != PACKET_VERSION:
        raise PacketError("unsupported spectrum packet version %d" % version)
    try:
        encoding = PayloadEncoding(encoding_code)
    except ValueError as error:
        raise PacketError("unknown spectrum payload encoding %d" % encoding_code) from error
    expected_payload_length = bin_count * encoding.bytes_per_bin
    if payload_length != expected_payload_length:
        raise PacketError("payload length does not match bin count and encoding")
    expected_length = PACKET_HEADER_BYTES + payload_length + PACKET_CRC_BYTES
    if len(raw) != expected_length:
        raise PacketError("spectrum packet length does not match its header")
    (wire_checksum,) = _PACKET_CRC.unpack(raw[-PACKET_CRC_BYTES:])
    actual_checksum = binascii.crc32(raw[:-PACKET_CRC_BYTES]) & 0xFFFFFFFF
    if wire_checksum != actual_checksum:
        raise PacketCRCError("spectrum packet CRC32 mismatch")
    payload = raw[PACKET_HEADER_BYTES:-PACKET_CRC_BYTES]
    return SpectrumPacket(
        sequence=sequence,
        channel=channel,
        fft_size=fft_size,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=center_frequency_hz,
        timestamp_ns=timestamp_ns,
        config_epoch=config_epoch,
        bin_start=bin_start,
        values_dbfs=_decode_payload(payload, encoding),
        encoding=encoding,
        dropped_frames=dropped_frames,
        overrun_events=overrun_events,
        dropped_updates=dropped_updates,
        flags=PacketFlag(flags),
        version=version,
    )


unpack_packet = unpack_spectrum_packet


@dataclass(frozen=True)
class OutputRateBudget:
    fft_size: int
    bin_count: int
    channels: int
    updates_per_second: float
    encoding: PayloadEncoding
    transport_budget_bytes_per_second: int

    @property
    def payload_bytes_per_update(self) -> int:
        return self.bin_count * self.channels * self.encoding.bytes_per_bin

    @property
    def payload_bytes_per_second(self) -> float:
        return self.payload_bytes_per_update * self.updates_per_second

    @property
    def wire_bytes_per_update(self) -> int:
        return self.payload_bytes_per_update + self.channels * PACKET_OVERHEAD_BYTES

    @property
    def wire_bytes_per_second(self) -> float:
        return self.wire_bytes_per_update * self.updates_per_second

    @property
    def payload_megabytes_per_second(self) -> float:
        return self.payload_bytes_per_second / 1_000_000.0

    @property
    def wire_megabytes_per_second(self) -> float:
        return self.wire_bytes_per_second / 1_000_000.0

    @property
    def utilization(self) -> float:
        return self.wire_bytes_per_second / self.transport_budget_bytes_per_second

    @property
    def fits(self) -> bool:
        return self.wire_bytes_per_second <= self.transport_budget_bytes_per_second

    @property
    def fits_vendor_claim(self) -> bool:
        return self.wire_bytes_per_second <= VENDOR_HOST_PAYLOAD_BYTES_PER_SECOND

    @property
    def required_bytes_per_second(self) -> float:
        """Complete packet stream, including headers and CRCs."""

        return self.wire_bytes_per_second

    def to_dict(self) -> Dict[str, object]:
        return {
            "fft_size": self.fft_size,
            "bin_count": self.bin_count,
            "channels": self.channels,
            "updates_per_second": self.updates_per_second,
            "encoding": self.encoding.name,
            "payload_bytes_per_second": self.payload_bytes_per_second,
            "wire_bytes_per_second": self.wire_bytes_per_second,
            "transport_budget_bytes_per_second": self.transport_budget_bytes_per_second,
            "utilization": self.utilization,
            "fits": self.fits,
        }


def calculate_output_rate_budget(
    fft_size: int,
    channels: int = 2,
    updates_per_second: float = 20.0,
    encoding: Union[PayloadEncoding, int, str] = PayloadEncoding.UINT16_LOG_POWER,
    bin_start: int = 0,
    bin_count: Optional[int] = None,
    transport_budget_bytes_per_second: int = VENDOR_HOST_PAYLOAD_BYTES_PER_SECOND,
) -> OutputRateBudget:
    if type(fft_size) is not int or not (
        MIN_FFT_SIZE <= fft_size <= MAX_FFT_SIZE
    ) or not _is_power_of_two(fft_size):
        raise ValueError("fft_size must be a supported power of two")
    if channels not in (1, 2):
        raise ValueError("channels must be one or two")
    if type(bin_start) is not int or not 0 <= bin_start < fft_size:
        raise ValueError("bin_start must address an FFT bin")
    selected = fft_size - bin_start if bin_count is None else bin_count
    if type(selected) is not int or selected <= 0 or bin_start + selected > fft_size:
        raise ValueError("selected bin range exceeds fft_size")
    if not isinstance(updates_per_second, (int, float)) or not math.isfinite(
        updates_per_second
    ) or updates_per_second <= 0:
        raise ValueError("updates_per_second must be finite and positive")
    if (
        type(transport_budget_bytes_per_second) is not int
        or transport_budget_bytes_per_second <= 0
    ):
        raise ValueError("transport budget must be a positive integer")
    selected_encoding = _coerce_enum(encoding, PayloadEncoding, "payload encoding")
    return OutputRateBudget(
        fft_size=fft_size,
        bin_count=selected,
        channels=channels,
        updates_per_second=float(updates_per_second),
        encoding=selected_encoding,  # type: ignore[arg-type]
        transport_budget_bytes_per_second=transport_budget_bytes_per_second,
    )


output_rate_budget = calculate_output_rate_budget


@dataclass(frozen=True)
class PipelineCounters:
    accepted_frames: int = 0
    emitted_updates: int = 0
    dropped_frames: int = 0
    overrun_events: int = 0
    dropped_updates: int = 0


@dataclass(frozen=True)
class FFTProcessResult:
    status: ProcessingStatus
    packets: Tuple[SpectrumPacket, ...]
    counters: PipelineCounters
    accepted_frames: int = 0
    dropped_frames: int = 0
    overrun_events: int = 0
    dropped_updates: int = 0
    accumulated_frames: int = 0
    reason: str = ""

    @property
    def emitted(self) -> bool:
        return bool(self.packets)


ProcessingResult = FFTProcessResult


class SpectrumProcessor:
    """Block-averaged, rate-limited multi-channel FFT result model."""

    def __init__(
        self,
        config: FFTConfig,
        pl_contract: Optional[PLThroughputContract] = None,
        initial_sequence: int = 0,
    ) -> None:
        _check_uint(initial_sequence, 64, "initial_sequence")
        if pl_contract is not None:
            assessment = pl_contract.assess(config)
            if not assessment.fits:
                raise ValueError("FFT configuration violates PL contract: %s" % "; ".join(assessment.reasons))
        self.config = config
        self.pl_contract = pl_contract
        self._sequence = initial_sequence
        self._linear_sums = [
            [0.0] * int(config.bin_count) for _ in range(config.channels)
        ]
        self._accumulated = 0
        self._frames_seen = 0
        self._counters = PipelineCounters()
        self._unreported_dropped_frames = 0
        self._unreported_overruns = 0
        self._unreported_dropped_updates = 0
        self._pending: Deque[Tuple[SpectrumPacket, ...]] = deque()

    @property
    def counters(self) -> PipelineCounters:
        return self._counters

    @property
    def pending_updates(self) -> int:
        return len(self._pending)

    def _replace_counters(self, **changes: int) -> None:
        current = self._counters
        values = {
            "accepted_frames": current.accepted_frames,
            "emitted_updates": current.emitted_updates,
            "dropped_frames": current.dropped_frames,
            "overrun_events": current.overrun_events,
            "dropped_updates": current.dropped_updates,
        }
        for name, delta in changes.items():
            values[name] += delta
        self._counters = PipelineCounters(**values)

    def _normalize_frames(
        self, frames: Sequence[Union[complex, Sequence[complex]]]
    ) -> Tuple[Sequence[complex], ...]:
        if self.config.channels == 1 and len(frames) == self.config.fft_size:
            first = frames[0]
            if not isinstance(first, (list, tuple)):
                return (frames,)  # type: ignore[return-value]
        if len(frames) != self.config.channels:
            raise ValueError("one IQ frame is required for each configured channel")
        normalized = tuple(frames)  # type: ignore[arg-type]
        if any(len(channel) != self.config.fft_size for channel in normalized):
            raise ValueError("IQ frame length does not match fft_size")
        return normalized

    def _default_timestamp(self) -> int:
        return (self._frames_seen * self.config.fft_size * 1_000_000_000) // self.config.sample_rate_hz

    def _result(
        self,
        status: ProcessingStatus,
        packets: Tuple[SpectrumPacket, ...] = (),
        accepted_frames: int = 0,
        dropped_frames: int = 0,
        overrun_events: int = 0,
        dropped_updates: int = 0,
        reason: str = "",
    ) -> FFTProcessResult:
        return FFTProcessResult(
            status=status,
            packets=packets,
            counters=self._counters,
            accepted_frames=accepted_frames,
            dropped_frames=dropped_frames,
            overrun_events=overrun_events,
            dropped_updates=dropped_updates,
            accumulated_frames=self._accumulated,
            reason=reason,
        )

    def record_overrun(self, reason: str = "IQ source overrun") -> FFTProcessResult:
        """Record an unusable input frame without silently substituting data."""

        self._frames_seen += 1
        self._replace_counters(dropped_frames=1, overrun_events=1)
        self._unreported_dropped_frames += 1
        self._unreported_overruns += 1
        if self.pl_contract is not None and self.pl_contract.overflow_policy is OverflowPolicy.STOP_AND_REPORT:
            self._reset_accumulation()
        return self._result(
            ProcessingStatus.OVERRUN,
            dropped_frames=1,
            overrun_events=1,
            reason=reason,
        )

    def _reset_accumulation(self) -> None:
        self._linear_sums = [
            [0.0] * int(self.config.bin_count) for _ in range(self.config.channels)
        ]
        self._accumulated = 0

    def drain(self, sink_ready: bool = True) -> FFTProcessResult:
        if not self._pending:
            return self._result(ProcessingStatus.ACCUMULATING, reason="no queued spectrum update")
        if not sink_ready:
            return self._result(ProcessingStatus.BACKPRESSURED, reason="spectrum sink is not ready")
        packets = self._pending.popleft()
        self._replace_counters(emitted_updates=1)
        return self._result(ProcessingStatus.EMITTED, packets=packets)

    def process_frame(
        self,
        frames: Sequence[Union[complex, Sequence[complex]]],
        timestamp_ns: Optional[int] = None,
        input_overrun: bool = False,
        sink_ready: bool = True,
    ) -> FFTProcessResult:
        if input_overrun:
            return self.record_overrun()
        channel_frames = self._normalize_frames(frames)
        timestamp = self._default_timestamp() if timestamp_ns is None else timestamp_ns
        _check_uint(timestamp, 64, "timestamp_ns")
        self._frames_seen += 1

        for channel, iq in enumerate(channel_frames):
            db_values = power_spectrum_dbfs(iq, self.config)
            for index, value_dbfs in enumerate(db_values):
                self._linear_sums[channel][index] += 10.0 ** (value_dbfs / 10.0)
        self._accumulated += 1
        self._replace_counters(accepted_frames=1)

        drained: Tuple[SpectrumPacket, ...] = ()
        if sink_ready and self._pending:
            drained = self._pending.popleft()
            self._replace_counters(emitted_updates=1)

        if self._accumulated < self.config.frames_per_update:
            if drained:
                return self._result(ProcessingStatus.EMITTED, packets=drained, accepted_frames=1)
            return self._result(ProcessingStatus.ACCUMULATING, accepted_frames=1)

        divisor = float(self._accumulated)
        averaged_dbfs = []
        floor_power = 10.0 ** (float(self.config.dbfs_floor) / 10.0)
        for channel_sums in self._linear_sums:
            averaged_dbfs.append(
                tuple(
                    10.0 * math.log10(max(total / divisor, floor_power))
                    for total in channel_sums
                )
            )
        packets = tuple(
            SpectrumPacket(
                sequence=self._sequence,
                channel=channel,
                fft_size=self.config.fft_size,
                sample_rate_hz=self.config.sample_rate_hz,
                center_frequency_hz=self.config.center_frequency_hz,
                timestamp_ns=timestamp,
                config_epoch=self.config.config_epoch,
                bin_start=self.config.bin_start,
                values_dbfs=values,
                encoding=self.config.payload_encoding,
                dropped_frames=self._unreported_dropped_frames,
                overrun_events=self._unreported_overruns,
                dropped_updates=self._unreported_dropped_updates,
            )
            for channel, values in enumerate(averaged_dbfs)
        )
        self._sequence = (self._sequence + 1) & ((1 << 64) - 1)
        self._unreported_dropped_frames = 0
        self._unreported_overruns = 0
        self._unreported_dropped_updates = 0
        self._reset_accumulation()

        if sink_ready:
            self._replace_counters(emitted_updates=1)
            return self._result(
                ProcessingStatus.EMITTED,
                packets=drained + packets,
                accepted_frames=1,
            )

        fifo_capacity = (
            self.pl_contract.result_fifo_updates
            if self.pl_contract is not None and self.pl_contract.output_can_backpressure
            else 0
        )
        if len(self._pending) < fifo_capacity:
            self._pending.append(packets)
            return self._result(
                ProcessingStatus.BACKPRESSURED,
                packets=drained,
                accepted_frames=1,
                reason="complete spectrum update queued until sink READY",
            )

        self._replace_counters(dropped_updates=1)
        self._unreported_dropped_updates += 1
        return self._result(
            ProcessingStatus.DROPPED,
            packets=drained,
            accepted_frames=1,
            dropped_updates=1,
            reason="spectrum result FIFO is full or backpressure is unsupported",
        )


FFTProcessor = SpectrumProcessor


__all__ = [
    "BackpressureMode",
    "FFTConfig",
    "FFTProcessResult",
    "FFTProcessor",
    "FFTWindow",
    "MAX_FFT_SIZE",
    "MIN_FFT_SIZE",
    "OutputRateBudget",
    "OverflowPolicy",
    "PACKET_CRC_BYTES",
    "PACKET_HEADER_BYTES",
    "PACKET_MAGIC",
    "PACKET_OVERHEAD_BYTES",
    "PACKET_VERSION",
    "PLResourceContract",
    "PLThroughputAssessment",
    "PLThroughputContract",
    "PacketCRCError",
    "PacketError",
    "PacketFlag",
    "PayloadEncoding",
    "PipelineCounters",
    "PowerAccumulator",
    "ProcessingResult",
    "ProcessingStatus",
    "SpectrumEncoding",
    "SpectrumPacket",
    "SpectrumProcessor",
    "UINT16_LOG_MIN_DBFS",
    "UINT16_LOG_STEP_DB",
    "VENDOR_HOST_PAYLOAD_BYTES_PER_SECOND",
    "Window",
    "calculate_output_rate_budget",
    "calculate_power_spectrum",
    "coherent_gain",
    "fftshift",
    "output_rate_budget",
    "pack_packet",
    "pack_spectrum_packet",
    "power_spectrum_dbfs",
    "radix2_fft",
    "reference_fft",
    "unpack_packet",
    "unpack_spectrum_packet",
    "window_coefficients",
    "windowed_fft",
]
