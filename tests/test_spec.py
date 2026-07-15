import copy
import unittest

from neptunesdr_twin.errors import ContractViolation
from neptunesdr_twin.spec import P210Spec


class P210SpecTests(unittest.TestCase):
    def test_resolved_listing_and_firmware_facts(self):
        spec = P210Spec.load_default()
        self.assertEqual(spec.processing["ddr_bytes"], 512 * 1024 * 1024)
        self.assertEqual(spec.processing["addressable_byte_bits"], 8)
        self.assertEqual(spec.processing["ddr_bus_bits"], 16)
        self.assertEqual(spec.processing["ddr_bus_bytes_per_transfer_beat"], 2)
        self.assertEqual(spec.processing["cpu_hz"], 666_666_687)
        self.assertEqual(spec.processing["cpu_hz_vendor_claim"], 766_000_000)
        self.assertEqual(spec.processing["ddr_clock_hz"], 533_333_374)
        self.assertEqual(spec.streaming["iq_significant_bits_per_component"], 12)
        self.assertEqual(spec.streaming["iq_container_bits_per_component"], 16)
        self.assertEqual(spec.streaming["complex_sample_bytes_per_channel"], 4)
        self.assertEqual(spec.streaming["rx_packer_scalar_lanes"], 4)
        self.assertEqual(spec.streaming["rx_dma_word_bits"], 64)
        self.assertEqual(spec.processing["qspi_bytes"], 32 * 1024 * 1024)
        self.assertEqual(spec.radio["transceiver"], "AD9361")
        self.assertEqual(spec.streaming["host_sustained_complex_samples_per_second"], 12_000_000)
        self.assertEqual(spec.streaming["burst_complex_samples_per_second"], 61_440_000)
        self.assertGreater(len(spec.unknowns), 0)

    def test_contradiction_is_preserved_with_resolution_basis(self):
        spec = P210Spec.load_default()
        conflict = spec.document["claim_conflicts"][0]
        self.assertEqual(conflict["listing_values"], [536870912, 1073741824])
        self.assertEqual(conflict["resolution"], 536870912)
        cpu_conflict = spec.document["claim_conflicts"][1]
        self.assertEqual(cpu_conflict["listing_values"], [766000000])
        self.assertEqual(cpu_conflict["resolution"], 666666687)

    def test_invalid_rate_contract_is_rejected(self):
        source = P210Spec.load_default().document
        broken = copy.deepcopy(source)
        broken["streaming"]["host_sustained_complex_samples_per_second"] = 70_000_000
        with self.assertRaises(ContractViolation):
            P210Spec(broken)


if __name__ == "__main__":
    unittest.main()
