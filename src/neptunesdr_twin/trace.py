"""Content-addressed event traces shared by protocol and RF models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    logical_ns: int
    contact: str
    direction: str
    event: str
    payload: Dict[str, Any]
    config_epoch: int = 0
    uncertainty_ns: int = 0


class TraceLog:
    """Append-only deterministic trace with canonical JSON serialization."""

    def __init__(self) -> None:
        self._events: List[TraceEvent] = []

    def append(
        self,
        *,
        logical_ns: int,
        contact: str,
        direction: str,
        event: str,
        payload: Optional[Dict[str, Any]] = None,
        config_epoch: int = 0,
        uncertainty_ns: int = 0,
    ) -> TraceEvent:
        item = TraceEvent(
            sequence=len(self._events),
            logical_ns=int(logical_ns),
            contact=contact,
            direction=direction,
            event=event,
            payload=dict(payload or {}),
            config_epoch=int(config_epoch),
            uncertainty_ns=int(uncertainty_ns),
        )
        self._events.append(item)
        return item

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterable[TraceEvent]:
        return iter(self._events)

    def canonical_bytes(self) -> bytes:
        lines = [
            json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
            for event in self._events
        ]
        return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def write_jsonl(self, path: Path) -> str:
        data = self.canonical_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

