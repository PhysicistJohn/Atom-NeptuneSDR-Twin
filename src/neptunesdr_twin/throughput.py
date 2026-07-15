"""Rate contracts that separate RF bandwidth from host transport bandwidth."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Dict, Optional

from .fft import (
    FFTConfig,
    PLThroughputContract,
    PayloadEncoding,
    calculate_output_rate_budget,
)


class Transport(str, Enum):
    P210_HOST_CLAIM = "p210-host-claim"
    USB2_THEORETICAL = "usb2-theoretical"
    GIGABIT_ETHERNET_THEORETICAL = "gigabit-ethernet-theoretical"
    INTERNAL_AD9361 = "internal-ad9361"


@dataclass(frozen=True)
class StreamRequest:
    sample_rate_hz: int
    channels: int = 2
    component_bits: int = 16
    components_per_sample: int = 2

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample rate must be positive")
        if self.channels not in (1, 2):
            raise ValueError("P210 stream channel count must be one or two")
        if self.component_bits not in (8, 12, 16):
            raise ValueError("unsupported I/Q component width")
        if self.components_per_sample != 2:
            raise ValueError("a complex stream has I and Q components")

    @property
    def container_bits(self) -> int:
        # Native libiio exposes the 12-bit converter words in 16-bit containers.
        return 16 if self.component_bits == 12 else self.component_bits

    @property
    def bytes_per_complex_sample(self) -> int:
        return self.components_per_sample * self.container_bits // 8

    @property
    def payload_bytes_per_second(self) -> int:
        return self.sample_rate_hz * self.channels * self.bytes_per_complex_sample

    @property
    def payload_bits_per_second(self) -> int:
        return self.payload_bytes_per_second * 8


@dataclass(frozen=True)
class TransportContract:
    transport: Transport
    payload_bytes_per_second: int
    evidence: str
    note: str

    def evaluate(self, request: StreamRequest) -> "RateAssessment":
        required = request.payload_bytes_per_second
        return RateAssessment(
            request=request,
            contract=self,
            fits=required <= self.payload_bytes_per_second,
            utilization=required / self.payload_bytes_per_second,
            maximum_sample_rate_hz=self.payload_bytes_per_second
            // (request.channels * request.bytes_per_complex_sample),
        )


@dataclass(frozen=True)
class RateAssessment:
    request: StreamRequest
    contract: TransportContract
    fits: bool
    utilization: float
    maximum_sample_rate_hz: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "transport": self.contract.transport.value,
            "fits": self.fits,
            "required_bytes_per_second": self.request.payload_bytes_per_second,
            "available_bytes_per_second": self.contract.payload_bytes_per_second,
            "utilization": self.utilization,
            "maximum_sample_rate_hz": self.maximum_sample_rate_hz,
            "evidence": self.contract.evidence,
            "note": self.contract.note,
        }


P210_HOST_12MSPS_ONE_CHANNEL = TransportContract(
    Transport.P210_HOST_CLAIM,
    payload_bytes_per_second=12_000_000 * 4,
    evidence="E0 vendor listing; channel and interface scope are not stated",
    note="12 MSPS converted to one native 16-bit I/Q stream; validate on the delivered unit",
)

USB2_THEORETICAL = TransportContract(
    Transport.USB2_THEORETICAL,
    payload_bytes_per_second=480_000_000 // 8,
    evidence="USB 2.0 signaling ceiling, not achievable application payload",
    note="Protocol, scheduling, controller, and software overhead make real payload lower",
)

GIGABIT_ETHERNET_THEORETICAL = TransportContract(
    Transport.GIGABIT_ETHERNET_THEORETICAL,
    payload_bytes_per_second=1_000_000_000 // 8,
    evidence="1000BASE-T line-rate ceiling, before Ethernet/IP/TCP/IIOD overhead",
    note="Real IIOD payload is lower and must be measured",
)

AD9361_INTERNAL = TransportContract(
    Transport.INTERNAL_AD9361,
    payload_bytes_per_second=61_440_000 * 2 * 4,
    evidence="AD9361/parallel-IQ model envelope for two native I/Q paths",
    note="Internal converter/FPGA contact, not a host-interface guarantee",
)


@dataclass(frozen=True)
class Wideband50MHzProfile:
    """Explicit acceptance profile for the user's 50 MHz requirement."""

    analog_bandwidth_hz: int = 50_000_000
    sample_rate_hz: int = 61_440_000
    channels: int = 2
    component_bits: int = 16

    @property
    def stream(self) -> StreamRequest:
        return StreamRequest(self.sample_rate_hz, self.channels, self.component_bits)

    def assess(self) -> Dict[str, object]:
        request = self.stream
        fft_config = FFTConfig(
            fft_size=65_536,
            channels=self.channels,
            sample_rate_hz=self.sample_rate_hz,
            update_rate_hz=20.0,
            payload_encoding=PayloadEncoding.UINT16_LOG_POWER,
        )
        fft_ingress = PLThroughputContract(
            stream_clock_hz=100_000_000,
            lanes=2,
            input_sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
            result_fifo_updates=2,
        ).assess(fft_config)
        spectrum_output = calculate_output_rate_budget(
            fft_config.fft_size,
            channels=fft_config.channels,
            updates_per_second=fft_config.effective_update_rate_hz,
            encoding=fft_config.payload_encoding,
        )
        return {
            "analog_bandwidth_hz": self.analog_bandwidth_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "raw_payload_bytes_per_second": request.payload_bytes_per_second,
            "internal": AD9361_INTERNAL.evaluate(request).to_dict(),
            "p210_host_claim": P210_HOST_12MSPS_ONE_CHANNEL.evaluate(request).to_dict(),
            "usb2_theoretical": USB2_THEORETICAL.evaluate(request).to_dict(),
            "gigabit_ethernet_theoretical": GIGABIT_ETHERNET_THEORETICAL.evaluate(
                request
            ).to_dict(),
            "on_chip_fft_profile": {
                "fft_size": fft_config.fft_size,
                "bin_resolution_hz": self.sample_rate_hz / fft_config.fft_size,
                "frames_per_update": fft_config.frames_per_update,
                "effective_update_rate_hz": fft_config.effective_update_rate_hz,
                "ingress_contract": fft_ingress.to_dict(),
                "spectrum_output": spectrum_output.to_dict(),
                "synthesis_and_timing_confirmation_required": True,
            },
            "continuous_host_strategy": (
                "process at 61.44 MSPS in PL and transmit framed spectra; decimate, select "
                "bins, channelize, or trigger before crossing USB/Ethernet; reserve DDR for "
                "short undecimated captures"
            ),
        }


def maximum_capture_seconds(memory_bytes: int, request: StreamRequest) -> float:
    if memory_bytes < 0:
        raise ValueError("memory_bytes must be non-negative")
    return memory_bytes / request.payload_bytes_per_second


def required_decimation(request: StreamRequest, contract: TransportContract) -> int:
    """Smallest integer rate reduction that satisfies a transport contract."""

    return max(1, math.ceil(request.payload_bytes_per_second / contract.payload_bytes_per_second))
