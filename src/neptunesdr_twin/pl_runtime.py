"""Continuous deterministic RF-to-NSFT programmable-logic runtime.

``SpectrumProcessor`` is the numerical golden model for one FFT input block.
This module supplies the missing dataflow semantics around it: it owns the RF
sample contact, consumes consecutive two-channel blocks, follows the live
AD9361 configuration, preserves paired NSFT updates, and turns downstream
backpressure into an input stall instead of an unreported loss.

Time in this runtime is sample time, not compute time.  The worker may fall
behind real time because the dependency-free reference FFT is intentionally
not an optimised implementation.  :meth:`ContinuousPLSpectrumRuntime.snapshot`
reports that lag explicitly.  A paced worker does not begin the next block
until wall time catches up with its last completed logical block boundary.

An AD9361 epoch change terminates an averaging interval.  Complete FFT blocks
which cannot meet the configured averaging depth under the old epoch are not
mixed into the new epoch: they are counted as discarded result frames and the
loss is carried in both packets of the next NSFT update.  Thus a retune is
atomic on the packet contact and never creates a silent discontinuity.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
import math
import threading
import time
from typing import Callable, Deque, Dict, Optional, Tuple

from .fft import FFTConfig, ProcessingStatus, SpectrumPacket, SpectrumProcessor
from .rf import IQFrame, RFModel


PacketPair = Tuple[SpectrumPacket, SpectrumPacket]
PublishCallback = Callable[[PacketPair], object]


class PLRuntimeError(RuntimeError):
    """Base class for virtual-PL runtime contract failures."""


class PLRuntimeContinuityError(PLRuntimeError):
    """The RF source did not deliver one consecutive, single-epoch block."""


class PLStepStatus(str, Enum):
    ACCUMULATING = "accumulating"
    PUBLISHED = "published"
    QUEUED = "queued"
    BACKPRESSURED = "backpressured"


@dataclass(frozen=True)
class PLRuntimeCounters:
    """Monotonic counters for every accepted, stalled, or discarded unit.

    ``iq_frames_consumed`` counts simultaneous 2x2 IQ sample instants.  An
    ``fft_frame`` is one ``fft_size``-sample block for both channels.  An
    ``update`` is one indivisible pair of NSFT channel packets.
    """

    iq_frames_consumed: int = 0
    fft_frames_processed: int = 0
    updates_generated: int = 0
    updates_published: int = 0
    updates_drained: int = 0
    publisher_attempts: int = 0
    publisher_rejections: int = 0
    publisher_errors: int = 0
    backpressure_events: int = 0
    backpressure_stalls: int = 0
    configuration_activations: int = 0
    reconfigurations: int = 0
    reconfiguration_discarded_fft_frames: int = 0
    reconfiguration_discarded_iq_frames: int = 0
    continuity_errors: int = 0
    runtime_errors: int = 0
    dropped_updates: int = 0
    pending_high_watermark: int = 0


@dataclass(frozen=True)
class PLStepResult:
    status: PLStepStatus
    packets: Tuple[SpectrumPacket, ...]
    counters: PLRuntimeCounters
    pending_updates: int
    reason: str = ""

    @property
    def emitted(self) -> bool:
        return bool(self.packets)


@dataclass(frozen=True)
class _RadioConfiguration:
    epoch: int
    sample_rate_hz: int
    center_frequency_hz: int


class ContinuousPLSpectrumRuntime:
    """Continuously transform an :class:`RFModel` into paired NSFT updates.

    The runtime is deterministic when driven with :meth:`step`.  :meth:`start`
    executes the exact same operation in a daemon worker.  A publisher accepts
    one complete packet pair at a time.  Returning the singleton ``False`` or
    raising an exception rejects that pair; all other return values acknowledge
    it.  Rejected pairs remain in a bounded queue and RF consumption stops
    before the queue could overflow, so ``dropped_updates`` remains zero.

    If ``publisher`` is absent, generated pairs are retained for
    :meth:`drain`.  This is a pull-mode sink with the same bounded semantics.
    """

    def __init__(
        self,
        rf: RFModel,
        config: FFTConfig,
        publisher: Optional[PublishCallback] = None,
        *,
        pending_update_capacity: int = 2,
        retry_interval_s: float = 0.005,
        realtime_pacing: bool = True,
        initial_sequence: int = 0,
    ) -> None:
        if not isinstance(rf, RFModel):
            raise TypeError("continuous PL runtime requires an RFModel")
        if not isinstance(config, FFTConfig):
            raise TypeError("continuous PL runtime requires an FFTConfig")
        if config.channels != 2:
            raise ValueError("continuous P210 PL runtime requires two FFT channels")
        if publisher is not None and not callable(publisher):
            raise TypeError("publisher must be callable or None")
        if type(pending_update_capacity) is not int or pending_update_capacity <= 0:
            raise ValueError("pending_update_capacity must be a positive integer")
        if (
            isinstance(retry_interval_s, bool)
            or not isinstance(retry_interval_s, (int, float))
            or not math.isfinite(float(retry_interval_s))
            or retry_interval_s <= 0
        ):
            raise ValueError("retry_interval_s must be finite and positive")
        if type(realtime_pacing) is not bool:
            raise TypeError("realtime_pacing must be a boolean")
        if type(initial_sequence) is not int or not 0 <= initial_sequence < (1 << 64):
            raise ValueError("initial_sequence must fit an unsigned 64-bit integer")

        self.rf = rf
        self.radio = rf.radio
        self.template = config
        self.publisher = publisher
        self.pending_update_capacity = pending_update_capacity
        self.retry_interval_s = float(retry_interval_s)
        self.realtime_pacing = realtime_pacing

        self._state_lock = threading.RLock()
        # Pull-mode drains share this boundary with processing.  Without it, a
        # second thread could remove a queued pair while a publisher callback
        # was accepting the same pair, delivering one update twice.
        self._step_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._configured_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker_busy = False
        self._pending: Deque[PacketPair] = deque()
        self._counter_values: Dict[str, int] = {
            field: 0 for field in PLRuntimeCounters.__dataclass_fields__
        }

        self._active_radio: Optional[_RadioConfiguration] = None
        self._active_config: Optional[FFTConfig] = None
        self._processor: Optional[SpectrumProcessor] = None
        self._next_sequence = initial_sequence
        self._frames_in_accumulation = 0
        self._accumulation_timestamp_ns: Optional[int] = None
        self._unreported_discarded_fft_frames = 0
        self._unreported_reconfiguration_events = 0
        self._expected_sample_index: Optional[int] = None
        self._logical_time_ns = int(self.rf.snapshot()["sample_time_ns"])
        self._last_error: Optional[str] = None
        self._terminal_error: Optional[str] = None
        self._backpressured = False

        now = time.monotonic_ns()
        self._wall_origin_ns = now
        self._logical_origin_ns = self._logical_time_ns
        self._wall_stop_ns: Optional[int] = now

    @property
    def counters(self) -> PLRuntimeCounters:
        with self._state_lock:
            return PLRuntimeCounters(**self._counter_values)

    @property
    def pending_updates(self) -> int:
        with self._state_lock:
            return len(self._pending)

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def active_config(self) -> Optional[FFTConfig]:
        with self._state_lock:
            return self._active_config

    def set_publisher(self, publisher: Optional[PublishCallback]) -> None:
        """Replace the non-blocking paired-update sink.

        Already queued updates retain order and are offered to the new sink
        before any subsequently generated update.
        """

        if publisher is not None and not callable(publisher):
            raise TypeError("publisher must be callable or None")
        with self._state_lock:
            self.publisher = publisher

    def start(self) -> "ContinuousPLSpectrumRuntime":
        """Start one paced continuous worker without resetting logical state."""

        with self._state_lock:
            if self.running:
                raise RuntimeError("continuous PL runtime is already running")
            if self._terminal_error is not None:
                raise PLRuntimeError(
                    "failed continuous PL runtime cannot be restarted: %s"
                    % self._terminal_error
                )
            self._stop_event.clear()
            self._last_error = None
            self._wall_origin_ns = time.monotonic_ns()
            self._logical_origin_ns = self._logical_time_ns
            self._wall_stop_ns = None
            self._thread = threading.Thread(
                target=self._run,
                name="neptune-pl-spectrum",
                daemon=True,
            )
            self._thread.start()
        return self

    def wait_configured(self, timeout_s: float = 1.0) -> bool:
        """Wait boundedly until the first radio/FFT configuration is active."""

        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or timeout_s < 0
        ):
            raise ValueError("timeout_s must be finite and non-negative")
        return self._configured_event.wait(float(timeout_s))

    def stop(self, timeout_s: float = 1.0) -> bool:
        """Request stop and wait no longer than ``timeout_s``.

        The boolean result is false if user publisher code or an FFT invocation
        is still executing at the deadline.  The worker remains daemonised and
        the condition is visible through :meth:`snapshot`; this method never
        claims an unbounded callback can be forcibly cancelled by Python.
        """

        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or timeout_s < 0
        ):
            raise ValueError("timeout_s must be finite and non-negative")
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(float(timeout_s))
        stopped = not bool(thread is not None and thread.is_alive())
        if stopped:
            with self._state_lock:
                if self._wall_stop_ns is None:
                    self._wall_stop_ns = time.monotonic_ns()
        return stopped

    close = stop

    def drain(self, maximum_updates: Optional[int] = None) -> Tuple[PacketPair, ...]:
        """Remove complete queued pairs for a pull-mode consumer."""

        if maximum_updates is not None and (
            type(maximum_updates) is not int or maximum_updates < 0
        ):
            raise ValueError("maximum_updates must be a non-negative integer or None")
        with self._step_lock:
            with self._state_lock:
                count = len(self._pending) if maximum_updates is None else min(
                    maximum_updates, len(self._pending)
                )
                pairs = tuple(self._pending.popleft() for _ in range(count))
                self._increment_locked(updates_drained=count)
                if len(self._pending) < self.pending_update_capacity:
                    self._backpressured = False
                return pairs

    def step(self) -> PLStepResult:
        """Consume at most one exact two-channel FFT block.

        Calling this from outside the worker while :meth:`start` is active is a
        programming error.  A backpressured result consumes no RF samples when
        the next update would exceed the bounded result queue.
        """

        if self.running and threading.current_thread() is not self._thread:
            raise RuntimeError("cannot step while the continuous worker is running")
        with self._step_lock:
            with self._state_lock:
                terminal_error = self._terminal_error
            if terminal_error is not None:
                raise PLRuntimeError(
                    "continuous PL runtime is failed: %s" % terminal_error
                )
            try:
                return self._step_locked()
            except Exception as error:
                description = "%s: %s" % (type(error).__name__, error)
                with self._state_lock:
                    if self._terminal_error is None:
                        self._terminal_error = description
                        if not isinstance(error, PLRuntimeContinuityError):
                            self._increment_locked(runtime_errors=1)
                    self._last_error = description
                raise

    def run_until_update(self, maximum_fft_frames: Optional[int] = None) -> PLStepResult:
        """Drive deterministic steps until an update or a sink stall occurs."""

        if maximum_fft_frames is not None and (
            type(maximum_fft_frames) is not int or maximum_fft_frames <= 0
        ):
            raise ValueError("maximum_fft_frames must be a positive integer or None")
        attempted = 0
        while maximum_fft_frames is None or attempted < maximum_fft_frames:
            result = self.step()
            if result.status is not PLStepStatus.ACCUMULATING:
                return result
            attempted += 1
        return result

    def snapshot(self) -> Dict[str, object]:
        """Return a non-blocking snapshot of the last completed PL boundary."""

        with self._state_lock:
            active = self._active_config
            thread = self._thread
            now = (
                time.monotonic_ns()
                if self._wall_stop_ns is None
                else self._wall_stop_ns
            )
            wall_elapsed = max(0, now - self._wall_origin_ns)
            logical_elapsed = max(0, self._logical_time_ns - self._logical_origin_ns)
            signed_lag = wall_elapsed - logical_elapsed
            return {
                "schema": 1,
                "running": bool(thread is not None and thread.is_alive()),
                "worker_busy": self._worker_busy,
                "logical_time_ns": self._logical_time_ns,
                "logical_elapsed_ns": logical_elapsed,
                "wall_elapsed_ns": wall_elapsed,
                "lag_ns": max(0, signed_lag),
                "lead_ns": max(0, -signed_lag),
                "signed_lag_ns": signed_lag,
                "expected_sample_index": self._expected_sample_index,
                "pending_updates": len(self._pending),
                "pending_update_capacity": self.pending_update_capacity,
                "backpressured": self._backpressured,
                "frames_in_accumulation": self._frames_in_accumulation,
                "next_sequence": self._next_sequence,
                "last_error": self._last_error,
                "failed": self._terminal_error is not None,
                "terminal_error": self._terminal_error,
                "configured": active is not None,
                "active_config": None
                if active is None
                else {
                    "fft_size": active.fft_size,
                    "channels": active.channels,
                    "sample_rate_hz": active.sample_rate_hz,
                    "center_frequency_hz": active.center_frequency_hz,
                    "config_epoch": active.config_epoch,
                    "frames_per_update": active.frames_per_update,
                    "effective_update_rate_hz": active.effective_update_rate_hz,
                    "bin_start": active.bin_start,
                    "bin_count": active.bin_count,
                },
                "counters": dict(self._counter_values),
            }

    def _step_locked(self) -> PLStepResult:
        with self._state_lock:
            self._worker_busy = True
        try:
            self._retry_oldest_pending()
            observed = self._stable_radio_configuration()
            self._remember_or_activate(observed)
            processor = self._processor
            active = self._active_config
            if processor is None or active is None:  # pragma: no cover - invariant guard
                raise PLRuntimeError("FFT processor was not activated")

            will_emit = self._frames_in_accumulation + 1 >= active.frames_per_update
            with self._state_lock:
                queue_full = len(self._pending) >= self.pending_update_capacity
            if will_emit and queue_full:
                self._enter_backpressure()
                return self._result(
                    PLStepStatus.BACKPRESSURED,
                    reason="complete NSFT pair queue is full; RF input is stalled",
                )

            frames = self.rf.synthesize(active.fft_size)
            radio_for_frames = self._validate_source_block(frames, observed)
            if radio_for_frames != self._active_radio:
                self._activate(radio_for_frames)
                processor = self._processor
                active = self._active_config
                if processor is None or active is None:  # pragma: no cover
                    raise PLRuntimeError("FFT processor was not reactivated")
                # A race may have moved to a configuration with a different
                # FFT averaging depth.  Capacity was checked pessimistically
                # only for the old epoch; never overflow if the new one emits.
                if len(self._pending) >= self.pending_update_capacity and (
                    self._frames_in_accumulation + 1 >= active.frames_per_update
                ):
                    self._record_unusable_block(frames)
                    raise PLRuntimeContinuityError(
                        "radio reconfigured while result queue was full"
                    )

            timestamp_ns = frames[0].timestamp_ns
            if timestamp_ns is None:
                self._record_continuity_error("RF block has no logical timestamp", frames)
            if self._accumulation_timestamp_ns is None:
                self._accumulation_timestamp_ns = int(timestamp_ns)

            channel0 = tuple(complex(frame.channel0) for frame in frames)
            channel1 = tuple(complex(frame.channel1) for frame in frames)
            result = processor.process_frame(
                (channel0, channel1),
                timestamp_ns=self._accumulation_timestamp_ns,
                sink_ready=True,
            )
            if result.status not in (ProcessingStatus.ACCUMULATING, ProcessingStatus.EMITTED):
                raise PLRuntimeError("unbounded numerical processor unexpectedly stalled")

            with self._state_lock:
                self._expected_sample_index = frames[-1].sample_index + 1
                self._logical_time_ns = int(self.rf.snapshot()["sample_time_ns"])
                self._frames_in_accumulation += 1
                self._increment_locked(
                    iq_frames_consumed=len(frames),
                    fft_frames_processed=1,
                )

            if not result.packets:
                self._leave_backpressure_if_possible()
                return self._result(PLStepStatus.ACCUMULATING)

            pair = self._checked_pair(result.packets)
            pair = self._decorate_reconfiguration_loss(pair)
            with self._state_lock:
                self._frames_in_accumulation = 0
                self._accumulation_timestamp_ns = None
                self._next_sequence = (pair[0].sequence + 1) & ((1 << 64) - 1)
                self._increment_locked(updates_generated=1)

            published = self._deliver(pair)
            if published:
                self._leave_backpressure_if_possible()
                return self._result(PLStepStatus.PUBLISHED, packets=pair)
            return self._result(
                PLStepStatus.QUEUED,
                packets=pair,
                reason="publisher did not accept the complete NSFT pair",
            )
        finally:
            with self._state_lock:
                self._worker_busy = False

    def _stable_radio_configuration(self) -> _RadioConfiguration:
        # A double epoch read is a seqlock-style guard around the fields used
        # in packet metadata.  AD9361 mutations bump config_epoch.
        for _ in range(100):
            before = int(self.radio.config_epoch)
            sample_rate = int(self.radio.sample_rate_hz)
            center = int(self.radio.rx_lo_hz)
            after = int(self.radio.config_epoch)
            if before == after:
                return _RadioConfiguration(after, sample_rate, center)
        raise PLRuntimeError("AD9361 configuration did not become stable")

    def _remember_or_activate(self, radio: _RadioConfiguration) -> None:
        if radio != self._active_radio:
            self._activate(radio)

    def _activate(self, radio: _RadioConfiguration) -> None:
        discarded = self._frames_in_accumulation
        old = self._active_radio
        if discarded:
            self._unreported_discarded_fft_frames += discarded
            self._unreported_reconfiguration_events += 1
        dynamic = replace(
            self.template,
            channels=2,
            sample_rate_hz=radio.sample_rate_hz,
            center_frequency_hz=radio.center_frequency_hz,
            config_epoch=radio.epoch,
        )
        processor = SpectrumProcessor(dynamic, initial_sequence=self._next_sequence)
        with self._state_lock:
            self._active_radio = radio
            self._active_config = dynamic
            self._processor = processor
            self._frames_in_accumulation = 0
            self._accumulation_timestamp_ns = None
            changes = {"configuration_activations": 1}
            if old is not None:
                changes["reconfigurations"] = 1
            if discarded:
                changes["reconfiguration_discarded_fft_frames"] = discarded
                changes["reconfiguration_discarded_iq_frames"] = (
                    discarded * self.template.fft_size
                )
            self._increment_locked(**changes)
            self._configured_event.set()

    def _validate_source_block(
        self,
        frames: Tuple[IQFrame, ...],
        expected_radio: _RadioConfiguration,
    ) -> _RadioConfiguration:
        if len(frames) != self.template.fft_size:
            self._record_continuity_error(
                "RF source returned %d samples for a %d-point FFT"
                % (len(frames), self.template.fft_size),
                frames,
            )
        start = frames[0].sample_index
        expected = self._expected_sample_index
        if expected is not None and start != expected:
            self._record_continuity_error(
                "RF sample discontinuity: expected %d, observed %d" % (expected, start),
                frames,
            )
        for offset, frame in enumerate(frames):
            if frame.sample_index != start + offset:
                self._record_continuity_error(
                    "RF block is not consecutive at offset %d" % offset,
                    frames,
                )
        epochs = {frame.config_epoch for frame in frames}
        if len(epochs) != 1:
            self._record_continuity_error(
                "AD9361 epoch changed inside one FFT block: %s" % sorted(epochs),
                frames,
            )
        epoch = int(next(iter(epochs)))
        if epoch == expected_radio.epoch:
            return expected_radio
        after = self._stable_radio_configuration()
        if after.epoch == epoch:
            return after
        self._record_continuity_error(
            "could not associate RF block epoch %d with stable radio metadata" % epoch,
            frames,
        )
        raise AssertionError("unreachable")  # pragma: no cover

    def _record_continuity_error(
        self, reason: str, frames: Tuple[IQFrame, ...]
    ) -> None:
        with self._state_lock:
            if frames:
                self._expected_sample_index = frames[-1].sample_index + 1
                self._logical_time_ns = int(self.rf.snapshot()["sample_time_ns"])
            self._last_error = reason
            self._increment_locked(
                continuity_errors=1,
                iq_frames_consumed=len(frames),
            )
        raise PLRuntimeContinuityError(reason)

    def _record_unusable_block(self, frames: Tuple[IQFrame, ...]) -> None:
        with self._state_lock:
            self._expected_sample_index = frames[-1].sample_index + 1
            self._logical_time_ns = int(self.rf.snapshot()["sample_time_ns"])
            self._last_error = "radio reconfigured while result queue was full"
            self._increment_locked(
                iq_frames_consumed=len(frames),
                continuity_errors=1,
            )

    @staticmethod
    def _checked_pair(packets: Tuple[SpectrumPacket, ...]) -> PacketPair:
        if len(packets) != 2:
            raise PLRuntimeError("one PL update must contain exactly two channel packets")
        pair = (packets[0], packets[1])
        if tuple(packet.channel for packet in pair) != (0, 1):
            raise PLRuntimeError("PL update channel order must be (0, 1)")
        shared_fields = (
            "sequence",
            "fft_size",
            "sample_rate_hz",
            "center_frequency_hz",
            "timestamp_ns",
            "config_epoch",
            "bin_start",
            "bin_count",
            "encoding",
            "dropped_frames",
            "overrun_events",
            "dropped_updates",
        )
        for field in shared_fields:
            if getattr(pair[0], field) != getattr(pair[1], field):
                raise PLRuntimeError("paired NSFT packets disagree on %s" % field)
        return pair

    def _decorate_reconfiguration_loss(self, pair: PacketPair) -> PacketPair:
        discarded = self._unreported_discarded_fft_frames
        events = self._unreported_reconfiguration_events
        if not discarded and not events:
            return pair
        if any(
            packet.dropped_frames + discarded >= (1 << 32)
            or packet.overrun_events + events >= (1 << 32)
            for packet in pair
        ):
            raise PLRuntimeError("unreported reconfiguration loss exceeds NSFT counters")
        decorated = tuple(
            replace(
                packet,
                dropped_frames=packet.dropped_frames + discarded,
                overrun_events=packet.overrun_events + events,
            )
            for packet in pair
        )
        self._unreported_discarded_fft_frames = 0
        self._unreported_reconfiguration_events = 0
        return self._checked_pair(decorated)  # type: ignore[arg-type]

    def _retry_oldest_pending(self) -> bool:
        with self._state_lock:
            if not self._pending or self.publisher is None:
                return False
            pair = self._pending[0]
            publisher = self.publisher
        accepted = self._call_publisher(publisher, pair)
        if accepted:
            with self._state_lock:
                # Only the step worker removes callback-bound entries.
                if self._pending and self._pending[0] is pair:
                    self._pending.popleft()
                    self._increment_locked(updates_published=1)
            self._leave_backpressure_if_possible()
        return accepted

    def _deliver(self, pair: PacketPair) -> bool:
        with self._state_lock:
            has_older = bool(self._pending)
            publisher = self.publisher
        if not has_older and publisher is not None:
            if self._call_publisher(publisher, pair):
                with self._state_lock:
                    self._increment_locked(updates_published=1)
                return True
        with self._state_lock:
            if len(self._pending) >= self.pending_update_capacity:
                # Capacity was reserved before RF consumption.  Reaching this
                # guard indicates an internal ordering bug; never count/drop.
                raise PLRuntimeError("paired result queue capacity invariant failed")
            self._pending.append(pair)
            self._counter_values["pending_high_watermark"] = max(
                self._counter_values["pending_high_watermark"], len(self._pending)
            )
        return False

    def _call_publisher(self, publisher: PublishCallback, pair: PacketPair) -> bool:
        with self._state_lock:
            self._increment_locked(publisher_attempts=1)
        try:
            accepted = publisher(pair) is not False
        except Exception as error:  # publisher failure is backpressure, not loss
            with self._state_lock:
                self._last_error = "%s: %s" % (type(error).__name__, error)
                self._increment_locked(publisher_errors=1)
            accepted = False
        if not accepted:
            with self._state_lock:
                self._increment_locked(publisher_rejections=1)
        return accepted

    def _enter_backpressure(self) -> None:
        with self._state_lock:
            changes = {"backpressure_stalls": 1}
            if not self._backpressured:
                self._backpressured = True
                changes["backpressure_events"] = 1
            self._increment_locked(**changes)

    def _leave_backpressure_if_possible(self) -> None:
        with self._state_lock:
            if len(self._pending) < self.pending_update_capacity:
                self._backpressured = False

    def _result(
        self,
        status: PLStepStatus,
        packets: Tuple[SpectrumPacket, ...] = (),
        reason: str = "",
    ) -> PLStepResult:
        with self._state_lock:
            return PLStepResult(
                status=status,
                packets=packets,
                counters=PLRuntimeCounters(**self._counter_values),
                pending_updates=len(self._pending),
                reason=reason,
            )

    def _increment_locked(self, **changes: int) -> None:
        for name, delta in changes.items():
            self._counter_values[name] += int(delta)

    def _pace(self) -> None:
        if not self.realtime_pacing:
            return
        with self._state_lock:
            logical_elapsed = max(0, self._logical_time_ns - self._logical_origin_ns)
            target = self._wall_origin_ns + logical_elapsed
        remaining_ns = target - time.monotonic_ns()
        if remaining_ns > 0:
            self._stop_event.wait(remaining_ns / 1_000_000_000.0)

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                result = self.step()
                if result.status is PLStepStatus.BACKPRESSURED:
                    self._stop_event.wait(self.retry_interval_s)
                else:
                    self._pace()
        except Exception as error:
            with self._state_lock:
                description = "%s: %s" % (type(error).__name__, error)
                self._last_error = description
                if self._terminal_error is None:
                    self._terminal_error = description
                    self._increment_locked(runtime_errors=1)
            self._stop_event.set()
        finally:
            with self._state_lock:
                self._worker_busy = False
                self._wall_stop_ns = time.monotonic_ns()

    def __enter__(self) -> "ContinuousPLSpectrumRuntime":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


# Concise public alias for composition code.
PLSpectrumRuntime = ContinuousPLSpectrumRuntime


__all__ = [
    "ContinuousPLSpectrumRuntime",
    "PacketPair",
    "PLRuntimeContinuityError",
    "PLRuntimeCounters",
    "PLRuntimeError",
    "PLSpectrumRuntime",
    "PLStepResult",
    "PLStepStatus",
    "PublishCallback",
]
