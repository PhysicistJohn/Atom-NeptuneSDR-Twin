import unittest

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.errors import InvalidTransition, OutOfRange
from neptunesdr_twin.zynq import AXIDMAC, BootSource, BootStage, SparseMemory, Zynq7020


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

    def test_dma_completion_and_irq_are_timed(self):
        completed = []
        dma = AXIDMAC(self.clock, "device_to_memory", on_transfer=lambda a, n, i: completed.append((a, n, i)))
        dma.write32(dma.REG_TRANSFER_ID, 3)
        dma.write32(dma.REG_DEST_ADDRESS, 0x1000)
        dma.write32(dma.REG_X_LENGTH, 1023)
        dma.write32(dma.REG_Y_LENGTH, 0)
        dma.write32(dma.REG_START_TRANSFER, 1)
        self.assertTrue(dma.busy)
        with self.assertRaises(InvalidTransition):
            dma.write32(dma.REG_START_TRANSFER, 1)
        self.clock.advance(1_000_000)
        self.assertEqual(completed, [(0x1000, 1024, 3)])
        self.assertFalse(dma.busy)
        self.assertEqual(dma.read32(dma.REG_TRANSFER_DONE), 1 << 3)


if __name__ == "__main__":
    unittest.main()
