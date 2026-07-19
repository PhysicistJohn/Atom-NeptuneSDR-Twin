"""Assume/guarantee contracts for the NeptuneSDR digital twin.

The module deliberately uses only the Python standard library.  Contracts are
small enough to load from JSON and strict enough to reject an incompatible
wiring.  This is not a general theorem prover.  It is a decidable contract
algebra for the value domains used at this device's typed subsystem
boundaries.  A failed proof is reported as "not proven" with a useful reason;
it is never silently treated as compatible.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


class ValueType(str, Enum):
    ANY = "any"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    NUMBER = "number"
    STRING = "string"
    BYTES = "bytes"
    COMPLEX = "complex"
    MAPPING = "mapping"
    SEQUENCE = "sequence"


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


class PortKind(str, Enum):
    RF = "rf"
    POWER = "power"
    CLOCK = "clock"
    RESET = "reset"
    GPIO = "gpio"
    SPI = "spi"
    IQ = "iq"
    AXI4_LITE = "axi4-lite"
    AXI4_STREAM = "axi4-stream"
    DMA = "dma"
    IRQ = "irq"
    USB = "usb"
    NETWORK = "network"
    CDC_ACM = "cdc-acm"
    BLOCK = "block"
    UART = "uart"
    IIO = "iio"
    APPLICATION = "application"


class EvidenceLevel(IntEnum):
    """Evidence is attached to individual guarantees, never just a project."""

    E0_CLAIM = 0
    E1_STATIC = 1
    E2_SIMULATION = 2
    E3_INTEGRATION = 3
    E4_DIFFERENTIAL = 4
    E5_CALIBRATED = 5

    @classmethod
    def parse(cls, value: Union["EvidenceLevel", str, int]) -> "EvidenceLevel":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value]
            except KeyError:
                try:
                    return cls(int(value))
                except (TypeError, ValueError):
                    raise ValueError("unknown evidence level: %r" % (value,))
        return cls(value)


@dataclass(frozen=True)
class DomainRelation:
    proven: bool
    reason: str = ""
    witness: Any = None


def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _kind_accepts(kind: ValueType, value: Any) -> bool:
    if kind is ValueType.ANY:
        return True
    if kind is ValueType.BOOL:
        return type(value) is bool
    if kind is ValueType.INT:
        return type(value) is int
    if kind is ValueType.FLOAT:
        return type(value) is float
    if kind is ValueType.NUMBER:
        return _is_real_number(value)
    if kind is ValueType.STRING:
        return isinstance(value, str)
    if kind is ValueType.BYTES:
        return isinstance(value, (bytes, bytearray, memoryview))
    if kind is ValueType.COMPLEX:
        return isinstance(value, complex) or _is_real_number(value)
    if kind is ValueType.MAPPING:
        return isinstance(value, Mapping)
    if kind is ValueType.SEQUENCE:
        return isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        )
    return False


def _kind_is_subset(inner: ValueType, outer: ValueType) -> bool:
    if outer is ValueType.ANY or inner is outer:
        return True
    if outer is ValueType.NUMBER and inner in (ValueType.INT, ValueType.FLOAT):
        return True
    if outer is ValueType.COMPLEX and inner in (
        ValueType.INT,
        ValueType.FLOAT,
        ValueType.NUMBER,
    ):
        return True
    return False


@dataclass(frozen=True)
class ValueDomain:
    """A monitorable set of values, including on-wire representation facts."""

    value_type: ValueType = ValueType.ANY
    unit: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    allowed: Tuple[Any, ...] = ()
    pattern: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    width_bits: Optional[int] = None
    signed: Optional[bool] = None
    byte_order: Optional[str] = None
    finite: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.value_type, ValueType):
            object.__setattr__(self, "value_type", ValueType(self.value_type))
        object.__setattr__(self, "allowed", tuple(self.allowed))
        for name in ("minimum", "maximum"):
            bound = getattr(self, name)
            if bound is not None and (
                not _is_real_number(bound) or not math.isfinite(bound)
            ):
                raise ValueError("%s must be a finite real number" % name)
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("minimum cannot exceed maximum")
        if self.min_length is not None and self.min_length < 0:
            raise ValueError("min_length cannot be negative")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError("max_length cannot be negative")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if self.width_bits is not None and self.width_bits <= 0:
            raise ValueError("width_bits must be positive")
        if self.byte_order not in (None, "little", "big", "native"):
            raise ValueError("byte_order must be little, big, native, or absent")
        if self.pattern is not None:
            re.compile(self.pattern)
        for value in self.allowed:
            error = self.validation_error(value, check_allowed=False)
            if error:
                raise ValueError("invalid allowed value %r: %s" % (value, error))

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "ValueDomain":
        raw = raw or {}
        return cls(
            value_type=ValueType(raw.get("value_type", "any")),
            unit=raw.get("unit"),
            minimum=raw.get("minimum"),
            maximum=raw.get("maximum"),
            allowed=tuple(raw.get("allowed", ())),
            pattern=raw.get("pattern"),
            min_length=raw.get("min_length"),
            max_length=raw.get("max_length"),
            width_bits=raw.get("width_bits"),
            signed=raw.get("signed"),
            byte_order=raw.get("byte_order"),
            finite=bool(raw.get("finite", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"value_type": self.value_type.value}
        for key in (
            "unit",
            "minimum",
            "maximum",
            "pattern",
            "min_length",
            "max_length",
            "width_bits",
            "signed",
            "byte_order",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.allowed:
            result["allowed"] = list(self.allowed)
        if not self.finite:
            result["finite"] = False
        return result

    def validation_error(self, value: Any, check_allowed: bool = True) -> Optional[str]:
        if not _kind_accepts(self.value_type, value):
            return "%r is not of type %s" % (value, self.value_type.value)
        if self.finite:
            if _is_real_number(value) and not math.isfinite(value):
                return "%r is not finite" % (value,)
            if isinstance(value, complex) and not (
                math.isfinite(value.real) and math.isfinite(value.imag)
            ):
                return "%r is not finite" % (value,)
        if check_allowed and self.allowed and value not in self.allowed:
            return "%r is not one of %r" % (value, self.allowed)
        if self.minimum is not None:
            if not _is_real_number(value) or value < self.minimum:
                return "%r is below minimum %r" % (value, self.minimum)
        if self.maximum is not None:
            if not _is_real_number(value) or value > self.maximum:
                return "%r is above maximum %r" % (value, self.maximum)
        if self.pattern is not None:
            if not isinstance(value, str) or re.fullmatch(self.pattern, value) is None:
                return "%r does not match %r" % (value, self.pattern)
        if self.min_length is not None:
            try:
                if len(value) < self.min_length:
                    return "length %d is below %d" % (len(value), self.min_length)
            except TypeError:
                return "%r has no length" % (value,)
        if self.max_length is not None:
            try:
                if len(value) > self.max_length:
                    return "length %d exceeds %d" % (len(value), self.max_length)
            except TypeError:
                return "%r has no length" % (value,)
        return None

    def contains(self, value: Any) -> bool:
        return self.validation_error(value) is None

    def subset_of(self, outer: "ValueDomain") -> DomainRelation:
        """Prove that every value in this domain is accepted by ``outer``."""

        if self.unit != outer.unit:
            if outer.unit is not None or self.unit is None:
                return DomainRelation(
                    False,
                    "unit %r is not compatible with %r" % (self.unit, outer.unit),
                    self.unit,
                )
        if not _kind_is_subset(self.value_type, outer.value_type):
            return DomainRelation(
                False,
                "type %s is not a subset of %s"
                % (self.value_type.value, outer.value_type.value),
            )
        for name in ("width_bits", "signed", "byte_order"):
            wanted = getattr(outer, name)
            actual = getattr(self, name)
            if wanted is not None and actual != wanted:
                return DomainRelation(
                    False,
                    "%s %r does not match required %r" % (name, actual, wanted),
                    actual,
                )

        # An explicit finite set is easy and gives useful counterexamples.
        if self.allowed:
            for value in self.allowed:
                error = outer.validation_error(value)
                if error:
                    return DomainRelation(False, error, value)
            return DomainRelation(True)

        # ``finite`` constrains the numeric values accepted by an otherwise
        # symbolic domain.  Bounds alone do not exclude NaN: both ``nan < x``
        # and ``nan > x`` are false.  Prove this direction explicitly instead
        # of silently treating a non-finite implementation domain as a subset
        # of a finite specification domain.
        if outer.finite and not self.finite:
            nonfinite_candidates: Tuple[Any, ...]
            if self.value_type is ValueType.COMPLEX:
                nonfinite_candidates = (
                    float("nan"),
                    float("inf"),
                    float("-inf"),
                    complex(float("nan"), 0.0),
                    complex(float("inf"), 0.0),
                    complex(0.0, float("inf")),
                )
            elif self.value_type in (
                ValueType.ANY,
                ValueType.FLOAT,
                ValueType.NUMBER,
            ):
                nonfinite_candidates = (
                    float("nan"),
                    float("inf"),
                    float("-inf"),
                )
            else:
                nonfinite_candidates = ()
            for value in nonfinite_candidates:
                if self.contains(value) and not outer.contains(value):
                    return DomainRelation(
                        False,
                        "non-finite values are not accepted by the outer domain",
                        value,
                    )

        if outer.allowed:
            candidates: Optional[Tuple[Any, ...]] = None
            if self.value_type is ValueType.BOOL:
                candidates = (False, True)
            if candidates is None:
                return DomainRelation(
                    False,
                    "an unconstrained domain cannot be proven inside a finite set",
                )
            for value in candidates:
                if self.contains(value) and value not in outer.allowed:
                    return DomainRelation(False, "value is outside allowed set", value)

        if outer.minimum is not None:
            if self.minimum is None or self.minimum < outer.minimum:
                return DomainRelation(
                    False,
                    "lower bound %r is below required %r"
                    % (self.minimum, outer.minimum),
                    self.minimum,
                )
        if outer.maximum is not None:
            if self.maximum is None or self.maximum > outer.maximum:
                return DomainRelation(
                    False,
                    "upper bound %r exceeds required %r"
                    % (self.maximum, outer.maximum),
                    self.maximum,
                )
        if outer.min_length is not None:
            if self.min_length is None or self.min_length < outer.min_length:
                return DomainRelation(False, "minimum length is not strong enough")
        if outer.max_length is not None:
            if self.max_length is None or self.max_length > outer.max_length:
                return DomainRelation(False, "maximum length is not strong enough")
        if outer.pattern is not None and self.pattern != outer.pattern:
            return DomainRelation(False, "regular-expression inclusion is not proven")
        return DomainRelation(True)


@dataclass(frozen=True)
class TypedPort:
    name: str
    kind: PortKind
    direction: PortDirection
    domain: ValueDomain = field(default_factory=ValueDomain)
    protocol: str = ""
    clock_domain: Optional[str] = None
    rate_hz: Optional[ValueDomain] = None
    external: bool = False
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("port name cannot be empty")
        if not isinstance(self.kind, PortKind):
            object.__setattr__(self, "kind", PortKind(self.kind))
        if not isinstance(self.direction, PortDirection):
            object.__setattr__(self, "direction", PortDirection(self.direction))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TypedPort":
        rate = raw.get("rate_hz")
        return cls(
            name=str(raw["name"]),
            kind=PortKind(raw["kind"]),
            direction=PortDirection(raw["direction"]),
            domain=ValueDomain.from_dict(raw.get("domain")),
            protocol=str(raw.get("protocol", "")),
            clock_domain=raw.get("clock_domain"),
            rate_hz=ValueDomain.from_dict(rate) if rate is not None else None,
            external=bool(raw.get("external", False)),
            required=bool(raw.get("required", True)),
            description=str(raw.get("description", "")),
        )


@dataclass(frozen=True)
class ContractClause:
    id: str
    key: str
    domain: ValueDomain
    description: str = ""
    modes: Tuple[str, ...] = ()
    external: bool = False
    required_evidence: EvidenceLevel = EvidenceLevel.E0_CLAIM
    depends_on: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.key:
            raise ValueError("clause id and key cannot be empty")
        object.__setattr__(self, "modes", tuple(self.modes))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        if not isinstance(self.required_evidence, EvidenceLevel):
            object.__setattr__(
                self,
                "required_evidence",
                EvidenceLevel.parse(self.required_evidence),
            )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractClause":
        return cls(
            id=str(raw["id"]),
            key=str(raw.get("key", raw["id"])),
            domain=ValueDomain.from_dict(raw.get("domain")),
            description=str(raw.get("description", "")),
            modes=tuple(raw.get("modes", ())),
            external=bool(raw.get("external", False)),
            required_evidence=EvidenceLevel.parse(
                raw.get("required_evidence", "E0_CLAIM")
            ),
            depends_on=tuple(raw.get("depends_on", ())),
        )


@dataclass(frozen=True)
class ModeTransition:
    source: str
    target: str
    trigger: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModeTransition":
        return cls(str(raw["source"]), str(raw["target"]), str(raw.get("trigger", "")))


@dataclass(frozen=True)
class EvidenceRecord:
    clause_id: str
    level: EvidenceLevel
    artifact: str = ""
    sha256: str = ""
    run_id: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.level, EvidenceLevel):
            object.__setattr__(self, "level", EvidenceLevel.parse(self.level))
        if self.sha256 and re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256) is None:
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceRecord":
        return cls(
            clause_id=str(raw["clause_id"]),
            level=EvidenceLevel.parse(raw["level"]),
            artifact=str(raw.get("artifact", "")),
            sha256=str(raw.get("sha256", "")),
            run_id=str(raw.get("run_id", "")),
            notes=str(raw.get("notes", "")),
        )


@dataclass(frozen=True)
class AssumeGuaranteeContract:
    component_id: str
    ports: Tuple[TypedPort, ...] = ()
    assumptions: Tuple[ContractClause, ...] = ()
    guarantees: Tuple[ContractClause, ...] = ()
    modes: Tuple[str, ...] = ()
    initial_mode: Optional[str] = None
    transitions: Tuple[ModeTransition, ...] = ()
    evidence: Tuple[EvidenceRecord, ...] = ()
    version: str = "1"
    description: str = ""
    refines: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.component_id:
            raise ValueError("component_id cannot be empty")
        for name in ("ports", "assumptions", "guarantees", "modes", "transitions", "evidence"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if len({p.name for p in self.ports}) != len(self.ports):
            raise ValueError("port names must be unique within a component")
        all_clauses = self.assumptions + self.guarantees
        if len({c.id for c in all_clauses}) != len(all_clauses):
            raise ValueError("clause ids must be unique within a component")
        if self.initial_mode is not None and self.initial_mode not in self.modes:
            raise ValueError("initial_mode must be declared in modes")
        for transition in self.transitions:
            if transition.source not in self.modes or transition.target not in self.modes:
                raise ValueError("transition refers to an undeclared mode")
        guarantee_ids = {c.id for c in self.guarantees}
        for record in self.evidence:
            if record.clause_id not in guarantee_ids:
                raise ValueError("evidence refers to unknown guarantee %s" % record.clause_id)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssumeGuaranteeContract":
        return cls(
            component_id=str(raw.get("component_id", raw.get("id", ""))),
            ports=tuple(TypedPort.from_dict(item) for item in raw.get("ports", ())),
            assumptions=tuple(
                ContractClause.from_dict(item) for item in raw.get("assumptions", ())
            ),
            guarantees=tuple(
                ContractClause.from_dict(item) for item in raw.get("guarantees", ())
            ),
            modes=tuple(raw.get("modes", ())),
            initial_mode=raw.get("initial_mode"),
            transitions=tuple(
                ModeTransition.from_dict(item) for item in raw.get("transitions", ())
            ),
            evidence=tuple(
                EvidenceRecord.from_dict(item) for item in raw.get("evidence", ())
            ),
            version=str(raw.get("version", "1")),
            description=str(raw.get("description", "")),
            refines=raw.get("refines"),
        )

    def port(self, name: str) -> TypedPort:
        for port in self.ports:
            if port.name == name:
                return port
        raise KeyError("component %s has no port %s" % (self.component_id, name))


@dataclass(frozen=True)
class Connection:
    source_component: str
    source_port: str
    target_component: str
    target_port: str
    adapter: Optional[str] = None
    delay_ticks: int = 0

    def __post_init__(self) -> None:
        if self.delay_ticks < 0:
            raise ValueError("delay_ticks cannot be negative")

    @property
    def source(self) -> str:
        return "%s.%s" % (self.source_component, self.source_port)

    @property
    def target(self) -> str:
        return "%s.%s" % (self.target_component, self.target_port)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Connection":
        if "source" in raw:
            source_component, source_port = str(raw["source"]).split(".", 1)
            target_component, target_port = str(raw["target"]).split(".", 1)
        else:
            source_component = str(raw["source_component"])
            source_port = str(raw["source_port"])
            target_component = str(raw["target_component"])
            target_port = str(raw["target_port"])
        return cls(
            source_component,
            source_port,
            target_component,
            target_port,
            raw.get("adapter"),
            int(raw.get("delay_ticks", 0)),
        )


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ContractIssue:
    code: str
    message: str
    path: str = ""
    severity: IssueSeverity = IssueSeverity.ERROR
    witness: Any = None


@dataclass(frozen=True)
class AssumptionBinding:
    consumer_component: str
    assumption_id: str
    provider_component: str
    guarantee_id: str


@dataclass(frozen=True)
class CompositionReport:
    name: str
    issues: Tuple[ContractIssue, ...] = ()
    bindings: Tuple[AssumptionBinding, ...] = ()
    composite: Optional[AssumeGuaranteeContract] = None

    @property
    def ok(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> Tuple[ContractIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.ERROR)

    @property
    def warnings(self) -> Tuple[ContractIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.WARNING)


def _clause_entails(stronger: ContractClause, weaker: ContractClause) -> DomainRelation:
    if stronger.key != weaker.key:
        return DomainRelation(False, "semantic keys differ")
    if not weaker.modes:
        if stronger.modes:
            return DomainRelation(False, "guarantee is not active in every required mode")
    elif stronger.modes and not set(weaker.modes).issubset(stronger.modes):
        return DomainRelation(False, "guarantee does not cover every required mode")
    return stronger.domain.subset_of(weaker.domain)


def _port_relation(source: TypedPort, target: TypedPort) -> Tuple[ContractIssue, ...]:
    issues: List[ContractIssue] = []
    if source.direction not in (PortDirection.OUTPUT, PortDirection.INOUT):
        issues.append(ContractIssue("bad-source-direction", "source port cannot drive a connection"))
    if target.direction not in (PortDirection.INPUT, PortDirection.INOUT):
        issues.append(ContractIssue("bad-target-direction", "target port cannot receive a connection"))
    if source.kind is not target.kind:
        issues.append(
            ContractIssue(
                "port-kind-mismatch",
                "%s cannot connect to %s" % (source.kind.value, target.kind.value),
            )
        )
    if source.protocol != target.protocol:
        issues.append(
            ContractIssue(
                "protocol-mismatch",
                "protocol %r does not match %r" % (source.protocol, target.protocol),
            )
        )
    relation = source.domain.subset_of(target.domain)
    if not relation.proven:
        issues.append(
            ContractIssue(
                "value-domain-mismatch",
                relation.reason,
                witness=relation.witness,
            )
        )
    if target.rate_hz is not None:
        if source.rate_hz is None:
            issues.append(
                ContractIssue("rate-not-declared", "source rate cannot be proven acceptable")
            )
        else:
            rate_relation = source.rate_hz.subset_of(target.rate_hz)
            if not rate_relation.proven:
                issues.append(
                    ContractIssue(
                        "rate-mismatch",
                        rate_relation.reason,
                        witness=rate_relation.witness,
                    )
                )
    return tuple(issues)


def compose_contracts(
    name: str,
    contracts: Iterable[AssumeGuaranteeContract],
    connections: Iterable[Connection],
) -> CompositionReport:
    components_list = list(contracts)
    connection_list = list(connections)
    issues: List[ContractIssue] = []
    bindings: List[AssumptionBinding] = []
    components: Dict[str, AssumeGuaranteeContract] = {}
    for component in components_list:
        if component.component_id in components:
            issues.append(
                ContractIssue("duplicate-component", "component id is not unique", component.component_id)
            )
        components[component.component_id] = component

    connected: set = set()
    driven_targets: set = set()
    for index, connection in enumerate(connection_list):
        path = "connections.%d" % index
        source_component = components.get(connection.source_component)
        target_component = components.get(connection.target_component)
        if source_component is None or target_component is None:
            issues.append(
                ContractIssue("unknown-component", "connection refers to an unknown component", path)
            )
            continue
        try:
            source_port = source_component.port(connection.source_port)
            target_port = target_component.port(connection.target_port)
        except KeyError as error:
            issues.append(ContractIssue("unknown-port", str(error), path))
            continue
        for issue in _port_relation(source_port, target_port):
            issues.append(replace(issue, path=path))
        if (
            source_port.clock_domain
            and target_port.clock_domain
            and source_port.clock_domain != target_port.clock_domain
            and not connection.adapter
        ):
            issues.append(
                ContractIssue(
                    "undeclared-clock-crossing",
                    "%s crosses to %s without a contracted adapter"
                    % (source_port.clock_domain, target_port.clock_domain),
                    path,
                )
            )
        target_key = (connection.target_component, connection.target_port)
        if target_key in driven_targets and target_port.direction is not PortDirection.INOUT:
            issues.append(ContractIssue("multiple-drivers", "target has more than one driver", path))
        driven_targets.add(target_key)
        connected.add((connection.source_component, connection.source_port))
        connected.add(target_key)

    for component in components_list:
        for port in component.ports:
            if port.required and not port.external and (component.component_id, port.name) not in connected:
                issues.append(
                    ContractIssue(
                        "unconnected-required-port",
                        "required internal port is not connected",
                        "%s.ports.%s" % (component.component_id, port.name),
                    )
                )

    all_guarantees: List[Tuple[str, ContractClause]] = [
        (component.component_id, guarantee)
        for component in components_list
        for guarantee in component.guarantees
    ]
    for consumer in components_list:
        for assumption in consumer.assumptions:
            if assumption.external:
                continue
            candidates: List[Tuple[str, ContractClause, DomainRelation]] = []
            for provider_id, guarantee in all_guarantees:
                if provider_id == consumer.component_id or guarantee.key != assumption.key:
                    continue
                candidates.append((provider_id, guarantee, _clause_entails(guarantee, assumption)))
            proven = [candidate for candidate in candidates if candidate[2].proven]
            if not proven:
                detail = ""
                if candidates:
                    detail = ": " + "; ".join(item[2].reason for item in candidates)
                issues.append(
                    ContractIssue(
                        "unsatisfied-assumption",
                        "no peer guarantee entails %s%s" % (assumption.key, detail),
                        "%s.assumptions.%s" % (consumer.component_id, assumption.id),
                    )
                )
            else:
                provider_id, guarantee, _ = proven[0]
                bindings.append(
                    AssumptionBinding(
                        consumer.component_id,
                        assumption.id,
                        provider_id,
                        guarantee.id,
                    )
                )

    external_ports: List[TypedPort] = []
    external_assumptions: List[ContractClause] = []
    composite_guarantees: List[ContractClause] = []
    composite_evidence: List[EvidenceRecord] = []
    for component in components_list:
        for port in component.ports:
            if port.external:
                external_ports.append(replace(port, name="%s.%s" % (component.component_id, port.name)))
        external_assumptions.extend(
            replace(clause, id="%s.%s" % (component.component_id, clause.id))
            for clause in component.assumptions
            if clause.external
        )
        composite_guarantees.extend(
            replace(clause, id="%s.%s" % (component.component_id, clause.id))
            for clause in component.guarantees
        )
        composite_evidence.extend(
            replace(record, clause_id="%s.%s" % (component.component_id, record.clause_id))
            for record in component.evidence
        )

    composite: Optional[AssumeGuaranteeContract] = None
    if not any(issue.severity is IssueSeverity.ERROR for issue in issues):
        composite = AssumeGuaranteeContract(
            component_id=name,
            ports=tuple(external_ports),
            assumptions=tuple(external_assumptions),
            guarantees=tuple(composite_guarantees),
            evidence=tuple(composite_evidence),
            description="Parallel composition with internal typed contacts hidden",
        )
    return CompositionReport(name, tuple(issues), tuple(bindings), composite)


@dataclass(frozen=True)
class ContractSystem:
    name: str
    contracts: Tuple[AssumeGuaranteeContract, ...]
    connections: Tuple[Connection, ...]
    schema_version: int = 1
    description: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractSystem":
        return cls(
            name=str(raw.get("name", raw.get("device", "system"))),
            contracts=tuple(
                AssumeGuaranteeContract.from_dict(item)
                for item in raw.get("components", raw.get("contracts", ()))
            ),
            connections=tuple(
                Connection.from_dict(item) for item in raw.get("connections", ())
            ),
            schema_version=int(raw.get("schema_version", 1)),
            description=str(raw.get("description", "")),
        )

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "ContractSystem":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def compose(self) -> CompositionReport:
        return compose_contracts(self.name, self.contracts, self.connections)


__all__ = [
    "AssumeGuaranteeContract",
    "AssumptionBinding",
    "CompositionReport",
    "Connection",
    "ContractClause",
    "ContractIssue",
    "ContractSystem",
    "DomainRelation",
    "EvidenceLevel",
    "EvidenceRecord",
    "IssueSeverity",
    "ModeTransition",
    "PortDirection",
    "PortKind",
    "TypedPort",
    "ValueDomain",
    "ValueType",
    "compose_contracts",
]
