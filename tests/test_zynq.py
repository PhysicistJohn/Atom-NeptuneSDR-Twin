import json
from pathlib import Path
import unittest

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.errors import OutOfRange
from neptunesdr_twin.zynq import AXIDMAC, BootSource, BootStage, SparseMemory, Zynq7020


ROOT = Path(__file__).resolve().parents[1]


class SparseMemoryTests(unittest.TestCase):
    def test_sparse_cross_page_access_and_bounds(self):
        memory = SparseMemory(8192)
        memory.write(4094, b"abcdef")
        self.assertEqual(memory.read(4094, 6), b"abcdef")
        self.assertEqual(memory.resident_pages, 2)
        self.assertEqual(memory.read(0, 4), b"\0" * 4)
        with self.assertRaises(OutOfRange):
            memory.write(8191, b"xx")


class ZynqTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.radio = AD9361(self.clock)
        self.zynq = Zynq7020(self.radio, self.clock)

    def test_observed_memory_map_and_uart(self):
        self.zynq.write(0x1000, b"payload")
        self.assertEqual(self.zynq.read(0x1000, 7), b"payload")
        self.zynq.write32(0xE0001030, ord("N"))
        self.assertEqual(self.zynq.uart.drain_tx(), b"N")
        self.assertEqual(self.zynq.read32(0x79020000), 0x00060000)
        with self.assertRaises(OutOfRange):
            self.zynq.read32(0xDEADBEEF)

    def test_spi_contact_reaches_ad9361(self):
        response = self.zynq.spi.transfer((0x037).to_bytes(2, "big") + b"\0")
        self.assertEqual(response[-1] & 0xF8, 0x08)

    def test_boot_timeline_and_failure(self):
        self.zynq.power_on(BootSource.SD)
        self.clock.advance(116_000_000)
        self.assertEqual(self.zynq.boot_stage, BootStage.RUNNING)
        self.zynq.power_off()
        self.zynq.power_on(BootSource.QSPI, kernel_available=False)
        self.clock.advance(116_000_000)
        self.assertEqual(self.zynq.boot_stage, BootStage.FAILED)

    def test_power_cycle_cancels_callbacks_from_previous_boot(self):
        completed = []
        self.zynq.power_on(BootSource.SD, kernel_available=True)
        self.zynq.rx_dma.on_transfer = (
            lambda address, length, transfer_id: completed.append(
                (address, length, transfer_id)
            )
        )
        self.zynq.rx_dma.write32(
            self.zynq.rx_dma.REG_CONTROL,
            self.zynq.rx_dma.CONTROL_ENABLE,
        )
        self.zynq.rx_dma.write32(self.zynq.rx_dma.REG_X_LENGTH, 491_519)
        self.zynq.rx_dma.write32(self.zynq.rx_dma.REG_START_TRANSFER, 1)
        self.clock.advance(500_000)
        self.zynq.power_off()
        self.assertEqual(self.clock.pending, 0)

        self.zynq.power_on(BootSource.QSPI, kernel_available=False)
        # The cancelled first boot would enter KERNEL at t=16 ms.  The new
        # boot's kernel decision is not due until t=16.5 ms.
        self.clock.run_until(16_250_000)
        self.assertEqual(self.zynq.boot_stage, BootStage.U_BOOT)
        self.clock.run_until(16_500_000)
        self.assertEqual(self.zynq.boot_stage, BootStage.FAILED)
        self.assertEqual(completed, [])

    def test_dma_xsa_capabilities_and_directional_registers(self):
        contact = json.loads(
            (ROOT / "cosim/p210-contacts.json").read_text()
        )["axi_dmac_probe"]
        rx = AXIDMAC(self.clock, "device_to_memory")
        tx = AXIDMAC(self.clock, "memory_to_device")

        self.assertEqual(AXIDMAC.VERSION, int(contact["version"], 0))
        self.assertEqual(AXIDMAC.DMA_LENGTH_WIDTH, contact["dma_length_width"])
        self.assertEqual(
            AXIDMAC.X_LENGTH_MASK, int(contact["x_length_write_mask"], 0)
        )
        self.assertEqual(
            AXIDMAC.MEMORY_ADDRESS_WIDTH,
            contact["memory_address_width_bits"],
        )
        self.assertEqual(
            AXIDMAC.MEMORY_BEAT_BYTES,
            contact["memory_address_alignment_bytes"],
        )
        self.assertEqual(
            AXIDMAC.ADDRESS_MASK,
            int(contact["memory_address_write_mask"], 0),
        )
        self.assertEqual(
            AXIDMAC.REG_CURRENT_DEST_ADDRESS,
            int(contact["current_destination_address_register"], 0),
        )
        self.assertEqual(
            AXIDMAC.REG_CURRENT_SRC_ADDRESS,
            int(contact["current_source_address_register"], 0),
        )
        self.assertEqual(AXIDMAC.SUPPORTS_2D, contact["two_dimensional_transfer"])
        self.assertEqual(AXIDMAC.QUEUE_DEPTH, contact["descriptor_queue_depth"])
        self.assertEqual(
            AXIDMAC.X_LENGTH_MASK + 1,
            contact["maximum_transfer_segment_bytes"],
        )
        self.assertEqual(
            AXIDMAC.IRQ_MASK_ALL, int(contact["irq_mask_reset"], 0)
        )
        self.assertEqual(rx.supports_cyclic, contact["rx_cyclic"])
        self.assertEqual(tx.supports_cyclic, contact["tx_cyclic"])
        self.assertEqual(
            rx.read32(rx.REG_FLAGS), int(contact["flags_reset"]["rx"], 0)
        )
        self.assertEqual(
            tx.read32(tx.REG_FLAGS), int(contact["flags_reset"]["tx"], 0)
        )

        for dma in (rx, tx):
            self.assertEqual(dma.read32(dma.REG_VERSION), 0x00040061)
            self.assertEqual(dma.read32(dma.REG_ID), 0)
            self.assertEqual(dma.read32(dma.REG_IRQ_MASK), 3)
            dma.write32(dma.REG_X_LENGTH, 0xFFFFFFFF)
            self.assertEqual(dma.read32(dma.REG_X_LENGTH), 0x00FFFFFF)
            for register in (
                dma.REG_Y_LENGTH,
                dma.REG_DEST_STRIDE,
                dma.REG_SRC_STRIDE,
            ):
                dma.write32(register, 0xFFFFFFFF)
                self.assertEqual(dma.read32(register), 0)

        self.assertEqual(rx.read32(rx.REG_FLAGS), rx.FLAG_TLAST)
        self.assertEqual(
            tx.read32(tx.REG_FLAGS), tx.FLAG_TLAST | tx.FLAG_CYCLIC
        )
        rx.write32(rx.REG_FLAGS, 0xFFFFFFFF)
        tx.write32(tx.REG_FLAGS, 0xFFFFFFFF)
        self.assertEqual(rx.read32(rx.REG_FLAGS), rx.FLAG_TLAST)
        self.assertEqual(
            tx.read32(tx.REG_FLAGS), tx.FLAG_TLAST | tx.FLAG_CYCLIC
        )

        rx.write32(rx.REG_DEST_ADDRESS, 0xFFFFFFFF)
        rx.write32(rx.REG_SRC_ADDRESS, 0xFFFFFFFF)
        tx.write32(tx.REG_DEST_ADDRESS, 0xFFFFFFFF)
        tx.write32(tx.REG_SRC_ADDRESS, 0xFFFFFFFF)
        self.assertEqual(rx.read32(rx.REG_DEST_ADDRESS), 0x1FFFFFF8)
        self.assertEqual(rx.read32(rx.REG_SRC_ADDRESS), 0)
        self.assertEqual(tx.read32(tx.REG_DEST_ADDRESS), 0)
        self.assertEqual(tx.read32(tx.REG_SRC_ADDRESS), 0x1FFFFFF8)

    def test_dma_completion_is_timed_and_ignores_unsupported_2d(self):
        completed = []
        dma = AXIDMAC(
            self.clock,
            "device_to_memory",
            on_transfer=lambda a, n, i: completed.append((a, n, i)),
        )
        dma.write32(dma.REG_DEST_ADDRESS, 0x1000)
        dma.write32(dma.REG_X_LENGTH, 1023)
        dma.write32(dma.REG_Y_LENGTH, 0xFFFFFFFF)

        # START_TRANSFER is ignored until CONTROL.ENABLE is asserted.
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertFalse(dma.busy)
        self.assertEqual(dma.read32(dma.REG_START_TRANSFER), 0)

        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertTrue(dma.busy)
        self.assertEqual(self.clock.advance(2_083), 0)
        self.assertEqual(self.clock.advance(1), 1)
        self.assertEqual(completed, [(0x1000, 1024, 0)])
        self.assertFalse(dma.busy)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 1)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_ID), 1)
        self.assertEqual(dma.read32(dma.REG_CURRENT_DEST_ADDRESS), 0x1400)

    def test_dma_irq_source_is_raw_and_pending_tracks_mask(self):
        dma = AXIDMAC(self.clock, "device_to_memory")
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        dma.write32(dma.REG_X_LENGTH, 0)
        dma.write32(dma.REG_START_TRANSFER, 1)

        self.assertEqual(
            dma.read32(dma.REG_IRQ_SOURCE), dma.IRQ_TRANSFER_QUEUED
        )
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), 0)
        dma.write32(dma.REG_IRQ_MASK, dma.IRQ_TRANSFER_COMPLETED)
        self.assertEqual(
            dma.read32(dma.REG_IRQ_PENDING), dma.IRQ_TRANSFER_QUEUED
        )
        dma.write32(dma.REG_IRQ_PENDING, dma.IRQ_TRANSFER_QUEUED)
        self.assertEqual(dma.read32(dma.REG_IRQ_SOURCE), 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), 0)

        self.assertEqual(self.clock.advance(3), 1)
        self.assertEqual(
            dma.read32(dma.REG_IRQ_SOURCE), dma.IRQ_TRANSFER_COMPLETED
        )
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), 0)
        dma.write32(dma.REG_IRQ_MASK, 0)
        self.assertEqual(
            dma.read32(dma.REG_IRQ_PENDING), dma.IRQ_TRANSFER_COMPLETED
        )
        dma.write32(dma.REG_IRQ_SOURCE, 0xFFFFFFFF)
        self.assertEqual(
            dma.read32(dma.REG_IRQ_SOURCE), dma.IRQ_TRANSFER_COMPLETED
        )
        dma.write32(dma.REG_IRQ_PENDING, dma.IRQ_TRANSFER_COMPLETED)
        self.assertEqual(dma.read32(dma.REG_IRQ_SOURCE), 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_PENDING), 0)

    def test_dma_four_entry_queue_ids_and_held_submission(self):
        completed = []
        dma = AXIDMAC(
            self.clock,
            "device_to_memory",
            on_transfer=lambda a, n, i: completed.append((a, n, i)),
        )
        dma.write32(
            dma.REG_CONTROL, dma.CONTROL_ENABLE | dma.CONTROL_PAUSE
        )

        for index in range(4):
            dma.write32(dma.REG_DEST_ADDRESS, 0x1000 + index * 8)
            dma.write32(dma.REG_X_LENGTH, 7)
            dma.write32(dma.REG_START_TRANSFER, 1)
            self.assertEqual(dma.read32(dma.REG_START_TRANSFER), 0)
        self.assertEqual(dma.queued_transfers, 4)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_ID), 0)
        self.assertEqual(self.clock.pending, 0)

        dma.write32(dma.REG_DEST_ADDRESS, 0x2000)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertEqual(dma.queued_transfers, 4)
        self.assertEqual(dma.read32(dma.REG_START_TRANSFER), 1)

        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        for _ in range(5):
            self.assertIsNotNone(self.clock.run_next())
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
        self.assertEqual(dma.queued_transfers, 0)
        self.assertFalse(dma.busy)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 0xF)

    def test_dma_pause_resume_preserves_remaining_delay(self):
        dma = AXIDMAC(self.clock, "device_to_memory")
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        dma.write32(dma.REG_X_LENGTH, 49_151)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.clock.advance(40_000)
        dma.write32(
            dma.REG_CONTROL, dma.CONTROL_ENABLE | dma.CONTROL_PAUSE
        )
        self.assertEqual(self.clock.pending, 0)
        self.clock.advance(1_000_000)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 0)

        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        self.assertEqual(self.clock.advance(59_999), 0)
        self.assertEqual(self.clock.advance(1), 1)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 1)

    def test_dma_reset_cancels_callback_and_restores_defaults(self):
        completed = []
        dma = AXIDMAC(
            self.clock,
            "device_to_memory",
            on_transfer=lambda a, n, i: completed.append((a, n, i)),
        )
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        dma.write32(dma.REG_X_LENGTH, 49_151)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertEqual(self.clock.pending, 1)

        dma.reset()
        self.assertEqual(self.clock.pending, 0)
        self.clock.advance(100_000)
        self.assertEqual(completed, [])
        self.assertFalse(dma.busy)
        self.assertEqual(dma.queued_transfers, 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_MASK), 3)
        self.assertEqual(dma.read32(dma.REG_FLAGS), dma.FLAG_TLAST)

    def test_tx_cyclic_repeats_without_ids_done_or_irqs(self):
        completed = []
        dma = AXIDMAC(
            self.clock,
            "memory_to_device",
            on_transfer=lambda a, n, i: completed.append((a, n, i)),
        )
        dma.write32(dma.REG_CONTROL, dma.CONTROL_ENABLE)
        dma.write32(dma.REG_SRC_ADDRESS, 0x1000)
        dma.write32(dma.REG_X_LENGTH, 7)
        dma.write32(dma.REG_START_TRANSFER, 1)

        self.assertEqual(dma.read32(dma.REG_TRANSFER_ID), 0)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_SOURCE), 0)
        self.assertIsNotNone(self.clock.run_next())
        self.assertEqual(completed, [(0x1000, 8, 0)])
        self.assertTrue(dma.busy)
        self.assertEqual(dma.queued_transfers, 1)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_ID), 0)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 0)
        self.assertEqual(dma.read32(dma.REG_IRQ_SOURCE), 0)
        self.assertEqual(dma.read32(dma.REG_CURRENT_SRC_ADDRESS), 0x1008)

        dma.write32(dma.REG_CONTROL, 0)
        self.assertEqual(self.clock.pending, 0)
        self.assertFalse(dma.busy)


if __name__ == "__main__":
    unittest.main()
