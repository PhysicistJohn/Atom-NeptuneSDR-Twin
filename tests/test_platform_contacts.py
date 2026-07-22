"""Compact platform proofs below the host protocol layer."""

import copy
import json
from pathlib import Path
import unittest

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.contracts import ContractSystem
from neptunesdr_twin.zynq import AXIDMAC, BootSource, BootStage, Zynq7020


ROOT = Path(__file__).resolve().parents[1]


class DecompositionContractTests(unittest.TestCase):
    def test_real_contract_graph_composes_and_a_broken_seam_fails_closed(self):
        raw = json.loads((ROOT / "specs" / "contracts.json").read_text(encoding="utf-8"))
        baseline = ContractSystem.from_dict(raw).compose()
        self.assertTrue(baseline.ok, baseline.issues)
        self.assertIsNotNone(baseline.composite)
        self.assertGreaterEqual(len(baseline.bindings), 27)

        broken = copy.deepcopy(raw)
        fft = next(item for item in broken["components"] if item["id"] == "pl_fft_pipeline")
        spectrum = next(port for port in fft["ports"] if port["name"] == "spectrum_out")
        spectrum["protocol"] = "nsft-v2-incompatible"
        rejected = ContractSystem.from_dict(broken).compose()
        self.assertFalse(rejected.ok)
        self.assertIsNone(rejected.composite)
        self.assertIn("protocol-mismatch", {issue.code for issue in rejected.errors})


class ZynqDMAContactTests(unittest.TestCase):
    def test_dma_queue_irq_completion_and_reset_are_one_coherent_contract(self):
        clock = VirtualClock()
        completed = []
        dma = AXIDMAC(
            clock,
            "device_to_memory",
            on_transfer=lambda address, length, transfer_id: completed.append(
                (address, length, transfer_id)
            ),
        )
        self.assertEqual(dma.read32(dma.REG_VERSION), 0x00040061)
        dma.write32(dma.REG_IRQ_MASK, 0)
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE | dma.CONTROL_PAUSE)
        for index in range(4):
            dma.write32(dma.REG_DEST_ADDRESS, 0x1000 + index * 8)
            dma.write32(dma.REG_X_LENGTH, 7)
            dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertEqual(dma.queued_transfers, 4)
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), dma.IRQ_TRANSFER_QUEUED)
        self.assertEqual(clock.pending, 0)

        dma.write32(dma.REG_DEST_ADDRESS, 0x2000)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertEqual(dma.queued_transfers, 4)
        self.assertEqual(dma.read32(dma.REG_START_TRANSFER), 1)
        dma.write32(dma.REG_IRQ_PENDING, dma.IRQ_TRANSFER_QUEUED)
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        for _ in range(5):
            self.assertIsNotNone(clock.run_next())
        self.assertEqual(
            completed,
            [
                (0x1000, 8, 0),
                (0x1008, 8, 1),
                (0x1010, 8, 2),
                (0x1018, 8, 3),
                (0x2000, 8, 0),
            ],
        )
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 0xF)
        # The fifth held submission becomes queued as the first descriptor
        # retires, so both raw causes must remain visible until W1C.
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), dma.IRQ_MASK_ALL)
        dma.write32(dma.REG_IRQ_PENDING, dma.IRQ_MASK_ALL)
        self.assertEqual(dma.read32(dma.REG_IRQ_SOURCE), 0)

        dma.reset()
        self.assertEqual(dma.queued_transfers, 0)
        self.assertFalse(dma.busy)
        self.assertEqual(clock.pending, 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_MASK), dma.IRQ_MASK_ALL)
        self.assertEqual(dma.read32(dma.REG_FLAGS), dma.FLAG_TLAST)

    def test_zynq_power_cycle_cancels_dma_and_stale_boot_callbacks(self):
        clock = VirtualClock()
        radio = AD9361(clock=clock)
        zynq = Zynq7020(radio, clock)
        completed = []
        zynq.power_on(BootSource.SD)
        zynq.rx_dma.on_transfer = lambda *values: completed.append(values)
        zynq.rx_dma.write32(zynq.rx_dma.REG_CONTROL, zynq.rx_dma.CONTROL_ENABLE)
        zynq.rx_dma.write32(zynq.rx_dma.REG_X_LENGTH, 491_519)
        zynq.rx_dma.write32(zynq.rx_dma.REG_START_TRANSFER, 1)
        clock.advance(500_000)
        zynq.power_off()
        self.assertEqual(clock.pending, 0)

        zynq.power_on(BootSource.QSPI, kernel_available=False)
        clock.advance(16_000_000)
        self.assertEqual(zynq.boot_stage, BootStage.FAILED)
        self.assertEqual(completed, [])


if __name__ == "__main__":
    unittest.main()
