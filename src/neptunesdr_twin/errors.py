"""Domain-specific failures exposed by the twin."""


class TwinError(Exception):
    """Base class for errors with defined digital-twin semantics."""


class ContractViolation(TwinError):
    """A component assumption, guarantee, or invariant was violated."""


class InvalidTransition(TwinError):
    """A requested state transition is not legal in the current mode."""


class OutOfRange(TwinError, ValueError):
    """A physical or digital control value is outside its declared domain."""


class USBProtocolError(TwinError):
    """A USB request or endpoint operation violates the active personality."""


class BufferOverrun(TwinError):
    """A bounded streaming contact could not accept more samples."""
