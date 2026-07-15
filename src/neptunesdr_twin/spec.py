"""Typed access and internal-consistency checks for the resolved P210 spec."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .errors import ContractViolation


@dataclass(frozen=True)
class RegionSpec:
    name: str
    base: int
    size: int
    dt_observed_size: Optional[int] = None

    @property
    def end(self) -> int:
        return self.base + self.size


class P210Spec:
    def __init__(self, document: Mapping[str, object]) -> None:
        self.document = dict(document)
        self.identity = dict(self.document["identity"])
        self.processing = dict(self.document["processing"])
        self.radio = dict(self.document["radio"])
        self.streaming = dict(self.document["streaming"])
        self.interfaces = dict(self.document["interfaces"])
        self.boot = dict(self.document["boot"])
        self.regions = tuple(
            RegionSpec(
                item["name"],
                int(item["base"]),
                int(item["size"]),
                int(item["dt_observed_size"]) if "dt_observed_size" in item else None,
            )
            for item in self.document["mmio"]
        )
        self.validate()

    @classmethod
    def load_default(cls) -> "P210Spec":
        path = Path(__file__).with_name("data") / "p210.json"
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def region(self, name: str) -> RegionSpec:
        for region in self.regions:
            if region.name == name:
                return region
        raise KeyError(name)

    @property
    def unknowns(self) -> Tuple[str, ...]:
        return tuple(self.document.get("unknown_until_capture", ()))

    def validate(self) -> None:
        failures = []
        if self.identity.get("product") != "P210":
            failures.append("identity.product must be P210")
        if self.processing.get("soc") != "XC7Z020-CLG400I":
            failures.append("resolved SoC must be XC7Z020-CLG400I")
        if int(self.processing.get("ddr_bytes", 0)) != 512 * 1024 * 1024:
            failures.append("resolved P210 DDR contract must be 512 MiB")
        if int(self.processing.get("addressable_byte_bits", 0)) != 8:
            failures.append("P210 software-visible bytes must be eight bits")
        if int(self.processing.get("ddr_bus_bits", 0)) != 16:
            failures.append("resolved P210 physical DDR bus must be 16 bits")
        if int(self.streaming.get("iq_container_bits_per_component", 0)) != 16:
            failures.append("I/Q DMA components must use 16-bit containers")
        if int(self.streaming.get("complex_sample_bytes_per_channel", 0)) != 4:
            failures.append("one I/Q sample per channel must occupy four bytes")
        if int(self.radio.get("rx_channels", 0)) != 2 or int(self.radio.get("tx_channels", 0)) != 2:
            failures.append("P210 must expose 2Rx/2Tx")
        if int(self.radio.get("board_min_frequency_hz", 0)) >= int(
            self.radio.get("board_max_frequency_hz", 0)
        ):
            failures.append("RF frequency interval is empty")
        if int(self.streaming.get("host_sustained_complex_samples_per_second", 0)) > int(
            self.streaming.get("burst_complex_samples_per_second", 0)
        ):
            failures.append("sustained host rate cannot exceed burst rate")
        for region in self.regions:
            if region.base < 0 or region.size <= 0 or region.end > (1 << 32):
                failures.append("invalid 32-bit region %s" % region.name)
        effective = sorted(self.regions, key=lambda item: item.base)
        for left, right in zip(effective, effective[1:]):
            if left.end > right.base:
                failures.append("MMIO regions %s and %s overlap" % (left.name, right.name))
        if failures:
            raise ContractViolation("; ".join(failures))

    def summary(self) -> Dict[str, object]:
        return {
            "identity": self.identity,
            "soc": self.processing["soc"],
            "cpu_hz": self.processing["cpu_hz"],
            "ddr_bytes": self.processing["ddr_bytes"],
            "ddr_bus_bits": self.processing["ddr_bus_bits"],
            "addressable_byte_bits": self.processing["addressable_byte_bits"],
            "rf": {
                "chip": self.radio["transceiver"],
                "channels": "%dT%dR" % (self.radio["tx_channels"], self.radio["rx_channels"]),
                "frequency_hz": [
                    self.radio["board_min_frequency_hz"],
                    self.radio["board_max_frequency_hz"],
                ],
                "bandwidth_hz": [self.radio["min_bandwidth_hz"], self.radio["max_bandwidth_hz"]],
            },
            "host_sustained_sps": self.streaming["host_sustained_complex_samples_per_second"],
            "burst_sps": self.streaming["burst_complex_samples_per_second"],
            "unknown_count": len(self.unknowns),
        }
