"""Deterministic virtual time used by every modeled subsystem.

No model sleeps or reads wall-clock time.  This makes calibration, USB reset,
DMA completion, and RF sample production reproducible and snapshot-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from typing import Callable, List, Optional, Tuple

from .errors import InvalidTransition


Callback = Callable[[], None]


@dataclass(order=True)
class _Scheduled:
    deadline_ns: int
    sequence: int
    callback: Callback = field(compare=False)
    label: str = field(default="", compare=False)
    cancelled: bool = field(default=False, compare=False)


class ScheduledHandle:
    """Cancellation handle for a virtual-time callback."""

    def __init__(self, event: _Scheduled) -> None:
        self._event = event

    @property
    def cancelled(self) -> bool:
        return self._event.cancelled

    def cancel(self) -> None:
        self._event.cancelled = True


class VirtualClock:
    """Monotonic nanosecond clock with stable FIFO ordering at equal times."""

    def __init__(self, start_ns: int = 0) -> None:
        if start_ns < 0:
            raise ValueError("start_ns must be non-negative")
        self._now_ns = int(start_ns)
        self._sequence = 0
        self._queue: List[_Scheduled] = []

    @property
    def now_ns(self) -> int:
        return self._now_ns

    @property
    def pending(self) -> int:
        return sum(not event.cancelled for event in self._queue)

    def schedule(self, delay_ns: int, callback: Callback, label: str = "") -> ScheduledHandle:
        if delay_ns < 0:
            raise ValueError("delay_ns must be non-negative")
        if not callable(callback):
            raise TypeError("callback must be callable")
        event = _Scheduled(self._now_ns + int(delay_ns), self._sequence, callback, label)
        self._sequence += 1
        heapq.heappush(self._queue, event)
        return ScheduledHandle(event)

    def call_at(self, deadline_ns: int, callback: Callback, label: str = "") -> ScheduledHandle:
        if deadline_ns < self._now_ns:
            raise InvalidTransition("cannot schedule an event in virtual-time history")
        return self.schedule(deadline_ns - self._now_ns, callback, label)

    def advance(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("virtual time cannot run backwards")
        return self.run_until(self._now_ns + int(delta_ns))

    def run_until(self, deadline_ns: int) -> int:
        if deadline_ns < self._now_ns:
            raise ValueError("virtual time cannot run backwards")
        executed = 0
        while self._queue and self._queue[0].deadline_ns <= deadline_ns:
            event = heapq.heappop(self._queue)
            self._now_ns = event.deadline_ns
            if event.cancelled:
                continue
            event.callback()
            executed += 1
        self._now_ns = int(deadline_ns)
        return executed

    def run_next(self) -> Optional[str]:
        while self._queue:
            event = heapq.heappop(self._queue)
            self._now_ns = event.deadline_ns
            if event.cancelled:
                continue
            event.callback()
            return event.label
        return None

    def deadlines(self) -> Tuple[Tuple[int, str], ...]:
        return tuple(
            (event.deadline_ns, event.label)
            for event in sorted(self._queue)
            if not event.cancelled
        )

