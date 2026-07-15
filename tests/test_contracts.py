import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from neptunesdr_twin.contracts import (  # noqa: E402
    AssumeGuaranteeContract,
    Connection,
    ContractClause,
    ContractSystem,
    EvidenceLevel,
    EvidenceRecord,
    ModeTransition,
    MonitorStatus,
    PortDirection,
    PortKind,
    RuntimeMonitor,
    TypedPort,
    ValueDomain,
    ValueType,
    check_evidence,
    check_refinement,
    compose_contracts,
)


def domain(minimum, maximum, unit=None):
    return ValueDomain(ValueType.NUMBER, unit=unit, minimum=minimum, maximum=maximum)


class ValueDomainTests(unittest.TestCase):
    def test_validation_is_strict_about_bool_and_integer(self):
        integer = ValueDomain(ValueType.INT, minimum=0, maximum=7)
        self.assertTrue(integer.contains(3))
        self.assertFalse(integer.contains(True))
        self.assertFalse(integer.contains(8))

    def test_subset_produces_counterexample(self):
        produced = domain(-1, 12, "V")
        accepted = domain(0, 10, "V")
        relation = produced.subset_of(accepted)
        self.assertFalse(relation.proven)
        self.assertEqual(relation.witness, -1)

    def test_finite_domain_and_wire_encoding_are_checked(self):
        source = ValueDomain(
            ValueType.INT,
            allowed=(0, 1),
            width_bits=1,
            signed=False,
            byte_order="little",
        )
        sink = ValueDomain(
            ValueType.INT,
            minimum=0,
            maximum=1,
            width_bits=1,
            signed=False,
            byte_order="little",
        )
        self.assertTrue(source.subset_of(sink).proven)
        wrong_width = ValueDomain(ValueType.INT, minimum=0, maximum=1, width_bits=8)
        self.assertFalse(source.subset_of(wrong_width).proven)

    def test_unproved_regex_inclusion_fails_closed(self):
        narrower = ValueDomain(ValueType.STRING, pattern="p210-[a-z]+")
        broader = ValueDomain(ValueType.STRING, pattern="p210-.*")
        relation = narrower.subset_of(broader)
        self.assertFalse(relation.proven)
        self.assertIn("not proven", relation.reason)


class CompositionTests(unittest.TestCase):
    def make_components(self):
        producer = AssumeGuaranteeContract(
            component_id="producer",
            ports=(
                TypedPort(
                    "out",
                    PortKind.IQ,
                    PortDirection.OUTPUT,
                    domain(1, 2, "V"),
                    protocol="test-iq",
                    clock_domain="sample_clk",
                ),
            ),
            guarantees=(
                ContractClause(
                    "ready",
                    "source.ready",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                ),
            ),
        )
        consumer = AssumeGuaranteeContract(
            component_id="consumer",
            ports=(
                TypedPort(
                    "in",
                    PortKind.IQ,
                    PortDirection.INPUT,
                    domain(0, 3, "V"),
                    protocol="test-iq",
                    clock_domain="sample_clk",
                ),
                TypedPort(
                    "api",
                    PortKind.APPLICATION,
                    PortDirection.OUTPUT,
                    ValueDomain(ValueType.MAPPING),
                    protocol="test-api",
                    external=True,
                ),
            ),
            assumptions=(
                ContractClause(
                    "source_ready",
                    "source.ready",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                ),
                ContractClause(
                    "ambient",
                    "ambient.c",
                    domain(-20, 70, "degC"),
                    external=True,
                ),
            ),
            guarantees=(
                ContractClause(
                    "api_ready",
                    "api.ready",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                ),
            ),
        )
        return producer, consumer

    def test_composition_binds_assumption_and_hides_internal_port(self):
        producer, consumer = self.make_components()
        report = compose_contracts(
            "system",
            (producer, consumer),
            (Connection("producer", "out", "consumer", "in"),),
        )
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(len(report.bindings), 1)
        self.assertIsNotNone(report.composite)
        self.assertEqual(
            [port.name for port in report.composite.ports],
            ["consumer.api"],
        )
        self.assertEqual(
            report.composite.assumptions[0].id,
            "consumer.ambient",
        )

    def test_composition_reports_domain_and_assumption_failures(self):
        producer, consumer = self.make_components()
        bad_output = TypedPort(
            "out",
            PortKind.IQ,
            PortDirection.OUTPUT,
            domain(-1, 4, "V"),
            protocol="test-iq",
            clock_domain="sample_clk",
        )
        producer = AssumeGuaranteeContract(
            "producer",
            ports=(bad_output,),
            guarantees=(
                ContractClause(
                    "not_the_fact",
                    "different.key",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                ),
            ),
        )
        report = compose_contracts(
            "bad",
            (producer, consumer),
            (Connection("producer", "out", "consumer", "in"),),
        )
        self.assertFalse(report.ok)
        codes = {issue.code for issue in report.issues}
        self.assertIn("value-domain-mismatch", codes)
        self.assertIn("unsatisfied-assumption", codes)
        self.assertIsNone(report.composite)

    def test_clock_crossing_requires_an_explicit_adapter(self):
        producer, consumer = self.make_components()
        consumer_port = TypedPort(
            "in",
            PortKind.IQ,
            PortDirection.INPUT,
            domain(0, 3, "V"),
            protocol="test-iq",
            clock_domain="other_clk",
        )
        consumer = AssumeGuaranteeContract(
            component_id=consumer.component_id,
            ports=(consumer_port, consumer.ports[1]),
            assumptions=consumer.assumptions,
            guarantees=consumer.guarantees,
        )
        report = compose_contracts(
            "crossing",
            (producer, consumer),
            (Connection("producer", "out", "consumer", "in"),),
        )
        self.assertIn("undeclared-clock-crossing", {i.code for i in report.issues})


class RefinementTests(unittest.TestCase):
    def make_contract(self, component_id, assumption_domain, guarantee_domain, evidence=()):
        return AssumeGuaranteeContract(
            component_id,
            assumptions=(ContractClause("input", "input.v", assumption_domain),),
            guarantees=(
                ContractClause(
                    "output",
                    "output.v",
                    guarantee_domain,
                    required_evidence=EvidenceLevel.E2_SIMULATION,
                ),
            ),
            evidence=evidence,
        )

    def test_weaker_assumption_and_stronger_guarantee_refine(self):
        specification = self.make_contract("spec", domain(0, 10), domain(0, 10))
        implementation = self.make_contract(
            "impl",
            domain(-5, 15),
            domain(2, 8),
            evidence=(EvidenceRecord("output", EvidenceLevel.E2_SIMULATION, "run.json"),),
        )
        report = check_refinement(implementation, specification, require_evidence=True)
        self.assertTrue(report.ok, report.issues)

    def test_stronger_assumption_and_weaker_guarantee_do_not_refine(self):
        specification = self.make_contract("spec", domain(0, 10), domain(0, 10))
        implementation = self.make_contract("impl", domain(2, 8), domain(-5, 15))
        report = check_refinement(implementation, specification)
        self.assertFalse(report.ok)
        codes = {issue.code for issue in report.errors}
        self.assertIn("stronger-assumption", codes)
        self.assertIn("weaker-guarantee", codes)

    def test_evidence_can_be_warning_or_hard_requirement(self):
        specification = self.make_contract("spec", domain(0, 10), domain(0, 10))
        implementation = self.make_contract("impl", domain(-1, 11), domain(1, 9))
        advisory = check_refinement(implementation, specification)
        strict = check_refinement(implementation, specification, require_evidence=True)
        self.assertTrue(advisory.ok)
        self.assertEqual(advisory.warnings[0].code, "insufficient-evidence")
        self.assertFalse(strict.ok)


class RuntimeMonitorTests(unittest.TestCase):
    def make_contract(self):
        return AssumeGuaranteeContract(
            "device",
            assumptions=(
                ContractClause("valid_vbus", "usb.vbus", domain(4.75, 5.25, "V")),
            ),
            guarantees=(
                ContractClause(
                    "enumerated",
                    "usb.enumerated",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                    modes=("runtime",),
                ),
            ),
            modes=("boot", "runtime"),
            initial_mode="boot",
            transitions=(ModeTransition("boot", "runtime", "ready"),),
        )

    def test_failed_assumption_blocks_guarantee_instead_of_passing_vacuously(self):
        monitor = RuntimeMonitor(self.make_contract())
        report = monitor.evaluate(timestamp=1.0)
        self.assertFalse(report.assumptions_satisfied)
        self.assertIsNone(report.guarantees_satisfied)
        self.assertEqual(report.guarantee_outcomes[0].status, MonitorStatus.INACTIVE)

        monitor.set_mode("runtime", "ready")
        report = monitor.evaluate(timestamp=2.0)
        self.assertEqual(report.guarantee_outcomes[0].status, MonitorStatus.BLOCKED)

    def test_runtime_failure_and_recovery_are_observable(self):
        monitor = RuntimeMonitor(self.make_contract())
        monitor.set_mode("runtime", "ready")
        monitor.update({"usb.vbus": 5.0, "usb.enumerated": False})
        failed = monitor.check(timestamp=10.0)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.guarantee_outcomes[0].status, MonitorStatus.FAIL)

        monitor.observe("usb.enumerated", True)
        passed = monitor.check(timestamp=11.0)
        self.assertTrue(passed.ok)
        self.assertEqual(len(monitor.history), 2)

    def test_illegal_mode_transition_is_rejected(self):
        monitor = RuntimeMonitor(self.make_contract())
        with self.assertRaises(ValueError):
            monitor.set_mode("runtime", "wrong-trigger")


class EvidenceTests(unittest.TestCase):
    def test_evidence_is_per_guarantee(self):
        contract = AssumeGuaranteeContract(
            "evidence",
            guarantees=(
                ContractClause(
                    "usb_descriptor",
                    "usb.descriptor.exact",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                    required_evidence=EvidenceLevel.E4_DIFFERENTIAL,
                ),
                ContractClause(
                    "rf_noise",
                    "rf.noise.calibrated",
                    ValueDomain(ValueType.BOOL, allowed=(True,)),
                    required_evidence=EvidenceLevel.E5_CALIBRATED,
                ),
            ),
            evidence=(
                EvidenceRecord("usb_descriptor", EvidenceLevel.E4_DIFFERENTIAL, "usb.pcapng"),
            ),
        )
        report = check_evidence(contract)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].path, "guarantees.rf_noise")


class P210SpecificationTests(unittest.TestCase):
    def test_decomposed_p210_contracts_load_and_compose(self):
        system = ContractSystem.from_json(ROOT / "specs" / "contracts.json")
        self.assertEqual(system.schema_version, 1)
        self.assertEqual(len(system.contracts), 8)
        self.assertIn("pl_fft_pipeline", {item.component_id for item in system.contracts})
        fft_contract = next(
            item for item in system.contracts if item.component_id == "pl_fft_pipeline"
        )
        self.assertEqual(fft_contract.port("spectrum_out").domain.max_length, 262_216)
        self.assertGreaterEqual(len(system.connections), 16)
        report = system.compose()
        self.assertTrue(report.ok, "\n".join(issue.message for issue in report.issues))
        self.assertIsNotNone(report.composite)
        self.assertGreaterEqual(len(report.bindings), 27)

    def test_exactness_claims_require_board_or_calibrated_evidence(self):
        system = ContractSystem.from_json(ROOT / "specs" / "contracts.json")
        required = [
            clause.required_evidence
            for contract in system.contracts
            for clause in contract.guarantees
        ]
        self.assertIn(EvidenceLevel.E4_DIFFERENTIAL, required)
        self.assertIn(EvidenceLevel.E5_CALIBRATED, required)


if __name__ == "__main__":
    unittest.main()
