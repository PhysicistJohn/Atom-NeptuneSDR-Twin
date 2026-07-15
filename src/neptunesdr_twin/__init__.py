"""Executable, contract-driven NeptuneSDR P210 digital twin."""

from .board import NeptuneSDRTwin
from .pl_runtime import (
    ContinuousPLSpectrumRuntime,
    PacketPair,
    PLRuntimeContinuityError,
    PLRuntimeCounters,
    PLRuntimeError,
    PLSpectrumRuntime,
    PLStepResult,
    PLStepStatus,
)
from .version import __version__

__all__ = [
    "ContinuousPLSpectrumRuntime",
    "NeptuneSDRTwin",
    "PacketPair",
    "PLRuntimeContinuityError",
    "PLRuntimeCounters",
    "PLRuntimeError",
    "PLSpectrumRuntime",
    "PLStepResult",
    "PLStepStatus",
    "__version__",
]
