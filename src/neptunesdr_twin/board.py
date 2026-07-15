"""Top-level P210 composition and lifecycle."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
from typing import Callable, Dict, Optional, Tuple

from .ad9361 import AD9361, ENSMState
from .clock import VirtualClock
from .fft import (
    FFTConfig,
    PLThroughputContract,
    SpectrumProcessor,
    calculate_output_rate_budget,
)
from .iio import IIOContext, IIODServer
from .pl_runtime import ContinuousPLSpectrumRuntime, PacketPair
from .rf import RFModel
from .spectrum_transport import SpectrumTCPPublisher
from .trace import TraceLog
from .usb import USBControlEndpoint, load_observed_usb_profile
from .usbip import USBIPServer
from .version import __version__
from .zynq import BootSource, BootStage, Zynq7020


class NeptuneSDRTwin:
    """Executable composition of the board's modeled subsystem contracts.

    The object is deterministic: all externally visible time advances only via
    :meth:`advance`.  Optional RF and USB models attach to this composition but
    the PS/PL, firmware lifecycle, SPI radio control, and IIOD endpoint remain
    usable independently for focused tests.
    """

    def __init__(
        self,
        *,
        serial: str = "P210TWIN000000000000000000000001",
        firmware_version: str = "p210-twin-" + __version__,
    ) -> None:
        self._lifecycle_lock = threading.RLock()
        self.clock = VirtualClock()
        self.trace = TraceLog()
        self.radio = AD9361(self.clock, self.trace)
        self.zynq = Zynq7020(self.radio, self.clock, self.trace)
        self.serial = serial
        self.firmware_version = firmware_version
        self.rf = RFModel(self.radio)
        self.usb = USBControlEndpoint(load_observed_usb_profile(), serial=serial)
        self.fft_contract = PLThroughputContract(
            stream_clock_hz=100_000_000,
            lanes=2,
            input_sample_rate_hz=61_440_000,
            channels=2,
            result_fifo_updates=2,
        )
        self.fft = SpectrumProcessor(
            FFTConfig(fft_size=65_536, channels=2),
            self.fft_contract,
        )
        self._spectrum_publisher: Optional[SpectrumTCPPublisher] = None
        self._pl_runtime: Optional[ContinuousPLSpectrumRuntime] = None
        self._last_pl_runtime_snapshot: Optional[Dict[str, object]] = None
        self._rx_sample_counter = 0
        self._tx_capture = bytearray()
        self.iio = IIOContext(
            self.radio,
            serial=serial,
            firmware_version=firmware_version,
            rx_provider=self._provide_rx,
            tx_consumer=self._consume_tx,
        )
        self._iiod: Optional[IIODServer] = None
        self._usbip: Optional[USBIPServer] = None

    @property
    def powered(self) -> bool:
        return self.zynq.boot_stage != BootStage.OFF

    def power_on(
        self,
        source: BootSource = BootSource.QSPI,
        *,
        kernel_available: bool = True,
    ) -> None:
        with self._lifecycle_lock:
            self.zynq.power_on(source, kernel_available=kernel_available)

            def initialize_radio() -> None:
                if self.zynq.boot_stage in {BootStage.KERNEL, BootStage.RUNNING}:
                    if self.radio.state == ENSMState.SLEEP:
                        self.radio.initialize()

            self.clock.schedule(50_000_000, initialize_radio, "ad9361-driver-probe")

    def power_off(self, runtime_timeout_s: float = 2.0) -> None:
        with self._lifecycle_lock:
            self._shutdown_services(runtime_timeout_s)
            self.zynq.power_off()
            self.radio.reset()
            self.usb.reset()

    def reset(self, source: Optional[BootSource] = None) -> None:
        with self._lifecycle_lock:
            selected = source or self.zynq.boot_source or BootSource.QSPI
            self.power_off()
            self.power_on(selected)

    def advance(self, delta_ns: int) -> int:
        with self._lifecycle_lock:
            return self.clock.advance(delta_ns)

    def boot_to_userspace(self, source: BootSource = BootSource.QSPI) -> None:
        with self._lifecycle_lock:
            self.power_on(source)
            self.advance(120_000_000)

    def start_iiod(self, host: str = "127.0.0.1", port: int = 30431) -> Tuple[str, int]:
        with self._lifecycle_lock:
            if self._iiod is not None:
                raise RuntimeError("IIOD endpoint is already running")
            self._iiod = IIODServer(self.iio, host, port).start()
            return self._iiod.address

    def stop_iiod(self) -> None:
        with self._lifecycle_lock:
            if self._iiod is not None:
                self._iiod.stop()
                self._iiod = None

    def start_usbip(
        self,
        host: str = "127.0.0.1",
        port: int = 3240,
        *,
        iiod_backend: Optional[Tuple[str, int]] = None,
    ) -> Tuple[str, int]:
        """Export the observed composite personality as a USB/IP device.

        Linux hosts can enumerate this contact with their standard USB/IP
        client.  Native libiio bulk pipes terminate in this twin's same IIO
        context, so USB and Ethernet control the identical radio state.
        """

        with self._lifecycle_lock:
            if self._usbip is not None:
                raise RuntimeError("USB/IP endpoint is already running")
            self._usbip = USBIPServer(
                self.usb,
                self.iio if iiod_backend is None else None,
                host,
                port,
                iiod_backend=iiod_backend,
            ).start()
            return self._usbip.address

    def stop_usbip(self) -> None:
        with self._lifecycle_lock:
            if self._usbip is not None:
                self._usbip.stop()
                self._usbip = None

    def start_spectrum_publisher(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> Tuple[str, int]:
        """Expose NSFT results over TCP (physical Ethernet or USB networking)."""

        with self._lifecycle_lock:
            if self._spectrum_publisher is not None:
                raise RuntimeError("spectrum publisher is already running")
            self._spectrum_publisher = SpectrumTCPPublisher(host, port).start()
            if self._pl_runtime is not None:
                self._pl_runtime.set_publisher(self._publish_spectrum_pair)
            return self._spectrum_publisher.address

    def stop_spectrum_publisher(self) -> None:
        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                self._pl_runtime.set_publisher(None)
            if self._spectrum_publisher is not None:
                self._spectrum_publisher.stop()
                self._spectrum_publisher = None

    def _publish_spectrum_pair(self, pair: PacketPair) -> bool:
        """Accept one atomic channel pair only when a TCP client received it."""

        publisher = self._spectrum_publisher
        return bool(publisher is not None and publisher.publish(pair) > 0)

    @property
    def continuous_spectrum(self) -> Optional[ContinuousPLSpectrumRuntime]:
        return self._pl_runtime

    def start_continuous_spectrum(
        self,
        config: Optional[FFTConfig] = None,
        *,
        publisher: Optional[Callable[[PacketPair], object]] = None,
        pending_update_capacity: int = 2,
        retry_interval_s: float = 0.005,
        realtime_pacing: bool = True,
    ) -> ContinuousPLSpectrumRuntime:
        """Start the direct RF-to-FFT virtual-PL dataflow.

        This runtime owns the RF sample contact while active.  It consumes
        consecutive simultaneous RX1/RX2 blocks, follows live AD9361
        rate/LO/epoch changes, and preserves complete paired NSFT updates under
        bounded downstream backpressure.  If no explicit callback is supplied,
        a running spectrum TCP publisher is used; otherwise results remain in
        the runtime's bounded pull queue.
        """

        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                raise RuntimeError("continuous PL spectrum runtime is already running")
            selected = config or self.fft.config
            sink = publisher
            if sink is None and self._spectrum_publisher is not None:
                sink = self._publish_spectrum_pair
            runtime = ContinuousPLSpectrumRuntime(
                self.rf,
                selected,
                sink,
                pending_update_capacity=pending_update_capacity,
                retry_interval_s=retry_interval_s,
                realtime_pacing=realtime_pacing,
            )
            self._last_pl_runtime_snapshot = None
            # Claim the shared RF sample contact before the worker starts.  An
            # in-flight IIOD read holds this same lifecycle boundary, and new
            # reads see the owner before thread creation.
            self._pl_runtime = runtime
            try:
                runtime.start()
            except Exception:
                self._pl_runtime = None
                raise
            return runtime

    def stop_continuous_spectrum(self, timeout_s: float = 2.0) -> bool:
        with self._lifecycle_lock:
            runtime = self._pl_runtime
            if runtime is None:
                return True
            stopped = runtime.stop(timeout_s)
            self._last_pl_runtime_snapshot = runtime.snapshot()
            if stopped:
                self._pl_runtime = None
            return stopped

    def process_fft_frame(self, frames, **kwargs):
        """Process one multi-channel frame and publish any complete update."""

        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                raise RuntimeError(
                    "manual FFT processing is unavailable while continuous PL owns "
                    "the spectrum stream"
                )
            result = self.fft.process_frame(frames, **kwargs)
            if result.packets and self._spectrum_publisher is not None:
                self._spectrum_publisher.publish(result.packets)
            return result

    def attach_rf(self, environment) -> None:
        """Attach an RF environment implementing ``receive_bytes``/``transmit_bytes``.

        Kept as a structural protocol rather than a base class so alternative
        calibrated or hardware-in-the-loop RF engines can replace the default.
        """

        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                raise RuntimeError("cannot replace RF while continuous PL runtime is active")
            self.rf = environment

    def attach_usb(self, gadget) -> None:
        with self._lifecycle_lock:
            if self._usbip is not None:
                raise RuntimeError("cannot replace USB while USB/IP export is active")
            self.usb = gadget

    def configure_fft(
        self,
        config: FFTConfig,
        contract: Optional[PLThroughputContract] = None,
    ) -> SpectrumProcessor:
        """Replace the PL spectrum pipeline at an explicit contract boundary.

        The Python processor is the golden numerical/wire-format model.  A
        synthesized implementation must refine the same ingress, overflow,
        packet, and rate guarantees; this method does not imply a resource or
        timing result for a particular Vivado FFT-IP configuration.
        """

        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                raise RuntimeError("cannot reconfigure FFT while continuous PL runtime is active")
            selected = contract or PLThroughputContract(
                stream_clock_hz=100_000_000,
                lanes=config.channels,
                input_sample_rate_hz=config.sample_rate_hz,
                channels=config.channels,
                result_fifo_updates=2,
            )
            assessment = selected.assess(config)
            if not assessment.fits:
                raise ValueError(
                    "FFT configuration violates the PL contact: "
                    + "; ".join(assessment.reasons)
                )
            self.fft_contract = selected
            self.fft = SpectrumProcessor(config, selected)
            return self.fft

    def drain_transmitted_bytes(self) -> bytes:
        data = bytes(self._tx_capture)
        self._tx_capture.clear()
        return data

    def snapshot(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "schema": 1,
            "logical_ns": self.clock.now_ns,
            "serial": self.serial,
            "firmware_version": self.firmware_version,
            "powered": self.powered,
            "zynq": self.zynq.snapshot(),
            "ad9361": self.radio.snapshot(),
            "iio_xml_sha256": hashlib.sha256(self.iio.xml().encode("utf-8")).hexdigest(),
            "fft": {
                "fft_size": self.fft.config.fft_size,
                "channels": self.fft.config.channels,
                "sample_rate_hz": self.fft.config.sample_rate_hz,
                "bin_count": self.fft.config.bin_count,
                "frames_per_update": self.fft.config.frames_per_update,
                "effective_update_rate_hz": self.fft.config.effective_update_rate_hz,
                "ingress": self.fft_contract.assess(self.fft.config).to_dict(),
                "egress": calculate_output_rate_budget(
                    self.fft.config.fft_size,
                    channels=self.fft.config.channels,
                    updates_per_second=self.fft.config.effective_update_rate_hz,
                    encoding=self.fft.config.payload_encoding,
                    bin_start=self.fft.config.bin_start,
                    bin_count=self.fft.config.bin_count,
                ).to_dict(),
                "counters": asdict(self.fft.counters),
            },
            "trace_sha256": self.trace.sha256(),
            "trace_events": len(self.trace),
            "tx_captured_bytes": len(self._tx_capture),
        }
        if self.rf is not None and hasattr(self.rf, "snapshot"):
            result["rf"] = self.rf.snapshot()
        if self.usb is not None and hasattr(self.usb, "snapshot"):
            result["usb"] = self.usb.snapshot()
        result["spectrum_transport"] = (
            self._spectrum_publisher.snapshot()
            if self._spectrum_publisher is not None
            else {"running": False, "address": None, "clients": 0}
        )
        result["usbip_transport"] = (
            self._usbip.snapshot()
            if self._usbip is not None
            else {"running": False, "address": None, "attached": False}
        )
        result["continuous_pl_spectrum"] = (
            self._pl_runtime.snapshot()
            if self._pl_runtime is not None
            else self._last_pl_runtime_snapshot
            or {"running": False, "configured": False}
        )
        return result

    def write_snapshot(self, path: Path) -> str:
        encoded = (json.dumps(self.snapshot(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        return hashlib.sha256(encoded).hexdigest()

    def _shutdown_services(self, runtime_timeout_s: float = 2.0) -> None:
        """Stop shared-contact workers before their transports and state.

        A user callback can outlive the first bounded PL stop attempt.  Closing
        its transport may release it, so the runtime gets one bounded join
        before and one after transport teardown.  If it still owns the RF
        source, callers receive an explicit error and :meth:`power_off` does
        not reset shared radio/Zynq state underneath that worker.
        """

        with self._lifecycle_lock:
            stopped = self.stop_continuous_spectrum(runtime_timeout_s)
            self.stop_spectrum_publisher()
            self.stop_usbip()
            self.stop_iiod()
            if not stopped:
                stopped = self.stop_continuous_spectrum(runtime_timeout_s)
            if not stopped:
                raise RuntimeError(
                    "continuous PL runtime did not stop before the bounded deadline"
                )

    def close(self, runtime_timeout_s: float = 2.0) -> None:
        self._shutdown_services(runtime_timeout_s)

    def __enter__(self) -> "NeptuneSDRTwin":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _provide_rx(self, length: int) -> bytes:
        with self._lifecycle_lock:
            if self._pl_runtime is not None:
                # IIOD maps PermissionError to -EPERM.  Control-plane operations,
                # including retunes, remain available; only the competing sample
                # consumer is rejected while the direct PL path owns continuity.
                raise PermissionError(
                    "continuous PL spectrum runtime owns the RF sample contact"
                )
            if self.rf is not None:
                if hasattr(self.rf, "receive_bytes"):
                    return bytes(self.rf.receive_bytes(length))
                if hasattr(self.rf, "stream_rx_bytes"):
                    return bytes(self.rf.stream_rx_bytes(length))
                if hasattr(self.rf, "read"):
                    return bytes(self.rf.read(length))
            self._rx_sample_counter += length // 8
            return b"\0" * length

    def _consume_tx(self, data: bytes) -> None:
        if self.rf is not None:
            if hasattr(self.rf, "transmit_bytes"):
                self.rf.transmit_bytes(data)
                return
            if hasattr(self.rf, "write_tx_bytes"):
                self.rf.write_tx_bytes(data)
                return
            if hasattr(self.rf, "write"):
                self.rf.write(data)
                return
        self._tx_capture.extend(data)
