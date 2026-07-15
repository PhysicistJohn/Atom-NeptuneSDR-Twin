"""Deterministic AD9361 control-plane and SPI behavioral model.

The model is deliberately register-visible: unimplemented registers retain
written values, while documented control registers have stateful side effects.
RF sample production lives in :mod:`neptunesdr_twin.rf` and consumes this
configuration.  This separation mirrors the SPI/control and CMOS-IQ contacts
on the physical board.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, IntEnum, IntFlag
from typing import Dict, Iterable, Optional, Set, Tuple

from .clock import ScheduledHandle, VirtualClock
from .errors import InvalidTransition, OutOfRange
from .trace import TraceLog


class ENSMState(IntEnum):
    SLEEP = 0x00
    ALERT = 0x05
    TX = 0x06
    RX = 0x08
    FDD = 0x0A


class GainMode(str, Enum):
    MANUAL = "manual"
    SLOW_ATTACK = "slow_attack"
    FAST_ATTACK = "fast_attack"
    HYBRID = "hybrid"


class Calibration(IntFlag):
    NONE = 0
    BB_DC = 1 << 0
    RF_DC = 1 << 1
    TX_MON = 1 << 2
    RX_GAIN_STEP = 1 << 3
    TX_QUAD = 1 << 4
    RX_QUAD = 1 << 5
    TX_BB_TUNE = 1 << 6
    RX_BB_TUNE = 1 << 7


@dataclass
class RxChannel:
    enabled: bool = True
    gain_db: float = 0.0
    gain_mode: GainMode = GainMode.SLOW_ATTACK
    rf_port: str = "A_BALANCED"


@dataclass
class TxChannel:
    enabled: bool = True
    attenuation_db: float = 10.0
    rf_port: str = "A"


class AD9361:
    """Two-channel AD9361 model with virtual-time calibration semantics."""

    REGISTER_COUNT = 0x400
    MAX_SPI_BURST = 8

    REG_SPI_CONF = 0x000
    REG_TX_ENABLE_FILTER_CTRL = 0x002
    REG_RX_ENABLE_FILTER_CTRL = 0x003
    REG_ENSM_MODE = 0x013
    REG_ENSM_CONFIG_1 = 0x014
    REG_CALIBRATION_CTRL = 0x016
    REG_STATE = 0x017
    REG_PRODUCT_ID = 0x037
    REG_CH_1_OVERFLOW = 0x05E
    REG_TX1_ATTEN_1 = 0x073
    REG_TX1_ATTEN_2 = 0x074
    REG_TX2_ATTEN_1 = 0x075
    REG_TX2_ATTEN_2 = 0x076
    REG_GAIN_RX1 = 0x2B0
    REG_GAIN_RX2 = 0x2B5
    REG_RX_CAL_STATUS = 0x244
    REG_RX_CP_OVERRANGE_VCO_LOCK = 0x247
    REG_TX_CAL_STATUS = 0x284
    REG_TX_CP_OVERRANGE_VCO_LOCK = 0x287

    PRODUCT_ID = 0x0A  # AD9361 family ID (0x08 mask) plus silicon revision 2.
    BBPLL_LOCK = 1 << 7
    CP_CAL_VALID = 1 << 7
    VCO_LOCK = 1 << 1
    MIN_CARRIER_HZ = 70_000_000
    MAX_CARRIER_HZ = 6_000_000_000
    MIN_SAMPLE_RATE_HZ = 520_833
    MAX_SAMPLE_RATE_HZ = 61_440_000
    MIN_BANDWIDTH_HZ = 200_000
    MAX_BANDWIDTH_HZ = 56_000_000
    RX_GAIN_RANGE_DB = (-3.0, 71.0)
    TX_ATTENUATION_RANGE_DB = (0.0, 89.75)
    CALIBRATION_LATENCY_NS = 1_000_000

    def __init__(
        self,
        clock: Optional[VirtualClock] = None,
        trace: Optional[TraceLog] = None,
    ) -> None:
        self.clock = clock or VirtualClock()
        self.trace = trace or TraceLog()
        self._registers = bytearray(self.REGISTER_COUNT)
        self._calibration_handle: Optional[ScheduledHandle] = None
        self._pending_calibrations = Calibration.NONE
        self.config_epoch = 0
        self.rx_channels = [RxChannel(), RxChannel()]
        self.tx_channels = [TxChannel(), TxChannel()]
        self.reset()

    def reset(self) -> None:
        if self._calibration_handle is not None:
            self._calibration_handle.cancel()
        self._registers = bytearray(self.REGISTER_COUNT)
        self._registers[self.REG_PRODUCT_ID] = self.PRODUCT_ID
        self.state = ENSMState.SLEEP
        self.rx_lo_hz = 2_400_000_000
        self.tx_lo_hz = 2_450_000_000
        self.sample_rate_hz = 30_720_000
        self.rx_bandwidth_hz = 18_000_000
        self.tx_bandwidth_hz = 18_000_000
        self.reference_clock_hz = 40_000_000
        self.rx_channels = [RxChannel(), RxChannel()]
        self.tx_channels = [TxChannel(), TxChannel()]
        self._pending_calibrations = Calibration.NONE
        self._calibration_handle = None
        self.config_epoch += 1
        self._sync_registers()
        self._record("reset", {})

    @property
    def pending_calibrations(self) -> Calibration:
        return self._pending_calibrations

    @property
    def calibrated(self) -> bool:
        return self._pending_calibrations == Calibration.NONE

    def initialize(self) -> None:
        if self.state != ENSMState.SLEEP:
            raise InvalidTransition("AD9361 initialization requires SLEEP state")
        self.set_ensm_state(ENSMState.ALERT)
        self.start_calibration(
            Calibration.BB_DC
            | Calibration.RF_DC
            | Calibration.TX_QUAD
            | Calibration.RX_QUAD
            | Calibration.TX_BB_TUNE
            | Calibration.RX_BB_TUNE
        )

    def set_ensm_state(self, target: ENSMState) -> None:
        target = ENSMState(target)
        legal: Dict[ENSMState, Set[ENSMState]] = {
            ENSMState.SLEEP: {ENSMState.ALERT},
            ENSMState.ALERT: {
                ENSMState.SLEEP,
                ENSMState.RX,
                ENSMState.TX,
                ENSMState.FDD,
            },
            ENSMState.RX: {ENSMState.ALERT},
            ENSMState.TX: {ENSMState.ALERT},
            ENSMState.FDD: {ENSMState.ALERT},
        }
        if target == self.state:
            return
        if target not in legal[self.state]:
            raise InvalidTransition("illegal ENSM transition %s -> %s" % (self.state.name, target.name))
        old = self.state
        self.state = target
        self._sync_registers()
        self._record("ensm", {"from": old.name, "to": target.name})

    def set_lo_frequency(self, direction: str, frequency_hz: int) -> None:
        frequency_hz = int(frequency_hz)
        self._in_range("LO frequency", frequency_hz, self.MIN_CARRIER_HZ, self.MAX_CARRIER_HZ)
        direction = direction.lower()
        if direction == "rx":
            changed = frequency_hz != self.rx_lo_hz
            self.rx_lo_hz = frequency_hz
            calibration = Calibration.RF_DC | Calibration.RX_QUAD
        elif direction == "tx":
            changed = frequency_hz != self.tx_lo_hz
            self.tx_lo_hz = frequency_hz
            calibration = Calibration.TX_QUAD
        else:
            raise ValueError("direction must be 'rx' or 'tx'")
        if changed:
            self._bump_epoch("lo_frequency", {"direction": direction, "frequency_hz": frequency_hz})
            self.start_calibration(calibration)

    def set_sample_rate(self, sample_rate_hz: int) -> None:
        sample_rate_hz = int(sample_rate_hz)
        self._in_range(
            "sample rate", sample_rate_hz, self.MIN_SAMPLE_RATE_HZ, self.MAX_SAMPLE_RATE_HZ
        )
        if self.rx_bandwidth_hz > sample_rate_hz or self.tx_bandwidth_hz > sample_rate_hz:
            raise OutOfRange("sample rate must be at least as large as both RF bandwidths")
        if sample_rate_hz != self.sample_rate_hz:
            self.sample_rate_hz = sample_rate_hz
            self._bump_epoch("sample_rate", {"sample_rate_hz": sample_rate_hz})
            self.start_calibration(Calibration.RX_BB_TUNE | Calibration.TX_BB_TUNE)

    def set_rf_bandwidth(self, direction: str, bandwidth_hz: int) -> None:
        bandwidth_hz = int(bandwidth_hz)
        self._in_range("RF bandwidth", bandwidth_hz, self.MIN_BANDWIDTH_HZ, self.MAX_BANDWIDTH_HZ)
        if bandwidth_hz > self.sample_rate_hz:
            raise OutOfRange("RF bandwidth cannot exceed the complex sample rate")
        direction = direction.lower()
        if direction == "rx":
            changed = bandwidth_hz != self.rx_bandwidth_hz
            self.rx_bandwidth_hz = bandwidth_hz
            calibration = Calibration.RX_BB_TUNE | Calibration.RX_QUAD
        elif direction == "tx":
            changed = bandwidth_hz != self.tx_bandwidth_hz
            self.tx_bandwidth_hz = bandwidth_hz
            calibration = Calibration.TX_BB_TUNE | Calibration.TX_QUAD
        else:
            raise ValueError("direction must be 'rx' or 'tx'")
        if changed:
            self._bump_epoch(
                "rf_bandwidth", {"direction": direction, "bandwidth_hz": bandwidth_hz}
            )
            self.start_calibration(calibration)

    def set_rx_gain(self, channel: int, gain_db: float) -> None:
        item = self._channel(self.rx_channels, channel)
        gain_db = float(gain_db)
        self._in_range("RX gain", gain_db, *self.RX_GAIN_RANGE_DB)
        if item.gain_mode != GainMode.MANUAL:
            raise InvalidTransition("hardware gain is writable only in manual gain mode")
        item.gain_db = gain_db
        self._registers[self.REG_GAIN_RX1 if channel == 0 else self.REG_GAIN_RX2] = int(
            round(gain_db - self.RX_GAIN_RANGE_DB[0])
        )
        self._bump_epoch("rx_gain", {"channel": channel, "gain_db": gain_db})

    def set_rx_gain_mode(self, channel: int, mode: GainMode) -> None:
        item = self._channel(self.rx_channels, channel)
        item.gain_mode = GainMode(mode)
        self._bump_epoch("rx_gain_mode", {"channel": channel, "mode": item.gain_mode.value})

    def set_tx_attenuation(self, channel: int, attenuation_db: float) -> None:
        item = self._channel(self.tx_channels, channel)
        attenuation_db = round(float(attenuation_db) * 4.0) / 4.0
        self._in_range("TX attenuation", attenuation_db, *self.TX_ATTENUATION_RANGE_DB)
        item.attenuation_db = attenuation_db
        word = int(round(attenuation_db * 4.0))
        register = self.REG_TX1_ATTEN_1 if channel == 0 else self.REG_TX2_ATTEN_1
        self._registers[register] = word & 0xFF
        self._registers[register + 1] = (word >> 8) & 0x01
        self._bump_epoch(
            "tx_attenuation", {"channel": channel, "attenuation_db": attenuation_db}
        )

    def start_calibration(self, calibrations: Calibration) -> None:
        calibrations = Calibration(calibrations)
        if calibrations == Calibration.NONE:
            return
        self._pending_calibrations |= calibrations
        self._registers[self.REG_CALIBRATION_CTRL] = int(self._pending_calibrations) & 0xFF
        if self._calibration_handle is not None:
            self._calibration_handle.cancel()
        pending_value = int(self._pending_calibrations)
        self._record("calibration_start", {"mask": pending_value})

        def complete() -> None:
            completed = int(self._pending_calibrations)
            self._pending_calibrations = Calibration.NONE
            self._registers[self.REG_CALIBRATION_CTRL] = 0
            self._calibration_handle = None
            self._sync_registers()
            self._record("calibration_complete", {"mask": completed})

        self._calibration_handle = self.clock.schedule(
            self.CALIBRATION_LATENCY_NS, complete, "ad9361-calibration"
        )
        self._sync_registers()

    def read_register(self, address: int) -> int:
        self._check_address(address)
        self._sync_registers()
        return self._registers[address]

    def write_register(self, address: int, value: int) -> None:
        self._check_address(address)
        if not 0 <= int(value) <= 0xFF:
            raise ValueError("register value must fit in one byte")
        value = int(value)
        if address == self.REG_PRODUCT_ID or address == self.REG_STATE:
            return  # Read-only silicon state.
        self._registers[address] = value
        if address == self.REG_SPI_CONF and (value & 0x81):
            self.reset()
            return
        if address == self.REG_CALIBRATION_CTRL:
            self.start_calibration(Calibration(value))
        elif address in (self.REG_ENSM_MODE, self.REG_ENSM_CONFIG_1):
            self._apply_ensm_registers()
        elif address in (
            self.REG_TX1_ATTEN_1,
            self.REG_TX1_ATTEN_2,
            self.REG_TX2_ATTEN_1,
            self.REG_TX2_ATTEN_2,
        ):
            channel = 0 if address <= self.REG_TX1_ATTEN_2 else 1
            base = self.REG_TX1_ATTEN_1 if channel == 0 else self.REG_TX2_ATTEN_1
            word = self._registers[base] | ((self._registers[base + 1] & 1) << 8)
            self.tx_channels[channel].attenuation_db = min(word / 4.0, 89.75)
        self._record("spi_write", {"address": address, "value": value})

    def spi_transfer(self, tx: bytes) -> bytes:
        """Execute the AD9361 16-bit instruction plus a 1..8 byte payload.

        Instruction bit 15 selects write (1), bits 14:12 encode count-1, and
        bits 9:0 contain the starting address.  Multi-byte accesses decrement
        the address, matching the ADI no-OS driver.
        """

        if len(tx) < 3:
            raise ValueError("SPI transaction requires a two-byte instruction and payload")
        instruction = (tx[0] << 8) | tx[1]
        write = bool(instruction & 0x8000)
        count = ((instruction >> 12) & 0x7) + 1
        address = instruction & 0x3FF
        if count > self.MAX_SPI_BURST or len(tx) != count + 2:
            raise ValueError("SPI payload length does not match instruction count")
        response = bytearray(len(tx))
        for offset in range(count):
            current = address - offset
            self._check_address(current)
            if write:
                self.write_register(current, tx[offset + 2])
            else:
                response[offset + 2] = self.read_register(current)
        self._record(
            "spi_transfer",
            {"write": write, "address": address, "count": count, "tx": tx.hex()},
        )
        return bytes(response)

    def snapshot(self) -> Dict[str, object]:
        return {
            "state": self.state.name,
            "rx_lo_hz": self.rx_lo_hz,
            "tx_lo_hz": self.tx_lo_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "rx_bandwidth_hz": self.rx_bandwidth_hz,
            "tx_bandwidth_hz": self.tx_bandwidth_hz,
            "reference_clock_hz": self.reference_clock_hz,
            "pending_calibrations": int(self.pending_calibrations),
            "config_epoch": self.config_epoch,
            "rx_channels": [
                {**asdict(channel), "gain_mode": channel.gain_mode.value}
                for channel in self.rx_channels
            ],
            "tx_channels": [asdict(channel) for channel in self.tx_channels],
            "registers_sha256_input": bytes(self._registers).hex(),
        }

    def _apply_ensm_registers(self) -> None:
        mode = self._registers[self.REG_ENSM_MODE]
        config = self._registers[self.REG_ENSM_CONFIG_1]
        if config & (1 << 2):
            target = ENSMState.ALERT
        elif config & (1 << 6):
            target = ENSMState.RX
        elif config & (1 << 5):
            target = ENSMState.TX
        elif mode & 1:
            target = ENSMState.FDD
        elif config & 1:
            target = ENSMState.ALERT
        else:
            return
        if target != self.state:
            try:
                self.set_ensm_state(target)
            except InvalidTransition:
                if self.state != ENSMState.ALERT:
                    self.set_ensm_state(ENSMState.ALERT)
                if target != ENSMState.ALERT:
                    self.set_ensm_state(target)

    def _sync_registers(self) -> None:
        calibration_state = 1 if self._pending_calibrations else 0
        self._registers[self.REG_STATE] = (calibration_state << 4) | (int(self.state) & 0x0F)
        self._registers[self.REG_PRODUCT_ID] = self.PRODUCT_ID
        # These are the readiness contacts polled by the unmodified ADI Linux
        # driver during probe.  They describe the settled virtual RF clocks;
        # REG_CALIBRATION_CTRL separately carries timed calibration progress.
        self._registers[self.REG_CH_1_OVERFLOW] |= self.BBPLL_LOCK
        self._registers[self.REG_RX_CAL_STATUS] |= self.CP_CAL_VALID
        self._registers[self.REG_TX_CAL_STATUS] |= self.CP_CAL_VALID
        self._registers[self.REG_RX_CP_OVERRANGE_VCO_LOCK] |= self.VCO_LOCK
        self._registers[self.REG_TX_CP_OVERRANGE_VCO_LOCK] |= self.VCO_LOCK
        tx_mask = sum((1 << index) for index, item in enumerate(self.tx_channels) if item.enabled)
        rx_mask = sum((1 << index) for index, item in enumerate(self.rx_channels) if item.enabled)
        self._registers[self.REG_TX_ENABLE_FILTER_CTRL] = (
            self._registers[self.REG_TX_ENABLE_FILTER_CTRL] & 0x3F
        ) | (tx_mask << 6)
        self._registers[self.REG_RX_ENABLE_FILTER_CTRL] = (
            self._registers[self.REG_RX_ENABLE_FILTER_CTRL] & 0x3F
        ) | (rx_mask << 6)

    def _bump_epoch(self, event: str, payload: Dict[str, object]) -> None:
        self.config_epoch += 1
        self._record(event, payload)

    def _record(self, event: str, payload: Dict[str, object]) -> None:
        self.trace.append(
            logical_ns=self.clock.now_ns,
            contact="ad9361.control",
            direction="internal",
            event=event,
            payload=payload,
            config_epoch=self.config_epoch,
        )

    @staticmethod
    def _in_range(label: str, value: float, minimum: float, maximum: float) -> None:
        if not minimum <= value <= maximum:
            raise OutOfRange("%s %s is outside [%s, %s]" % (label, value, minimum, maximum))

    @staticmethod
    def _channel(channels: Iterable[object], channel: int):
        values = list(channels)
        if channel not in (0, 1):
            raise IndexError("AD9361 channel must be 0 or 1")
        return values[channel]

    @classmethod
    def _check_address(cls, address: int) -> None:
        if not 0 <= int(address) < cls.REGISTER_COUNT:
            raise OutOfRange("AD9361 register address must be in [0x000, 0x3ff]")
