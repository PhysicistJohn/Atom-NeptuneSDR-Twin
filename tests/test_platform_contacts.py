"""Compact platform proofs below the host protocol layer."""

import copy
import io
import json
from pathlib import Path
import struct
import unittest
import zipfile
import zlib

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.contracts import ContractSystem
from neptunesdr_twin.errors import FirmwareFormatError
from neptunesdr_twin.firmware import UIMAGE_MAGIC, UImage
from neptunesdr_twin.runtime_rootfs import CpioEntry, NewcArchive
from neptunesdr_twin.xsa import validate_xsa
from neptunesdr_twin.zynq import AXIDMAC, BootSource, BootStage, Zynq7020


ROOT = Path(__file__).resolve().parents[1]


def _entry(name, data=b"", mode=0o100644, inode=1):
    return CpioEntry(name, inode, mode, 0, 0, 1, 0, data)


def _uimage(payload):
    name = b"P210 test kernel".ljust(32, b"\0")
    header = bytearray(
        struct.pack(
            ">7I4B32s",
            UIMAGE_MAGIC,
            0,
            1,
            len(payload),
            0x8000,
            0x8000,
            zlib.crc32(payload) & 0xFFFFFFFF,
            5,
            2,
            2,
            0,
            name,
        )
    )
    struct.pack_into(">I", header, 4, zlib.crc32(header) & 0xFFFFFFFF)
    return bytes(header) + payload


def _xsa(extra_member=None):
    part = "xc7z020clg400-1"
    metadata = {
        "hardware": "true",
        "generatedVersion": "2023.2",
        "generatedTimestamp": "test",
        "devices": [{"part": {"name": part}}],
    }
    modules = (
        ("sys_ps7", "xilinx.com:ip:processing_system7:5.5"),
        ("axi_ad9361", "analog.com:user:axi_ad9361:1.0"),
        ("axi_ad9361_adc_dma", "analog.com:user:axi_dmac:1.0"),
        ("axi_ad9361_dac_dma", "analog.com:user:axi_dmac:1.0"),
        ("cpack", "analog.com:user:util_cpack2:1.0"),
        ("tx_upack", "analog.com:user:util_upack2:1.0"),
    )
    parameters = {
        "sys_ps7": {
            "PCW_ACT_APU_PERIPHERAL_FREQMHZ": "666.666687",
            "PCW_UIPARAM_DDR_BUS_WIDTH": "16 Bit",
            "PCW_UIPARAM_ACT_DDR_FREQ_MHZ": "533.333374",
            "PCW_UIPARAM_DDR_PARTNO": "MT41K256M16 RE-125",
            "PCW_CLK0_FREQ": "100000000",
        },
        "cpack": {
            "NUM_OF_CHANNELS": "4",
            "SAMPLE_DATA_WIDTH": "16",
            "SAMPLES_PER_CHANNEL": "1",
        },
        "axi_ad9361_adc_dma": {
            "DMA_DATA_WIDTH_SRC": "64",
            "DMA_DATA_WIDTH_DEST": "64",
        },
    }
    module_xml = "".join(
        '<MODULE INSTANCE="%s" VLNV="%s">%s</MODULE>'
        % (
            instance,
            vlnv,
            "".join(
                '<PARAMETER NAME="%s" VALUE="%s" />' % item
                for item in parameters.get(instance, {}).items()
            ),
        )
        for instance, vlnv in modules
    )
    ranges = (
        ("axi_ad9361", 0x79020000, 0x7902FFFF),
        ("axi_ad9361_adc_dma", 0x7C400000, 0x7C400FFF),
        ("axi_ad9361_dac_dma", 0x7C420000, 0x7C420FFF),
    )
    range_xml = "".join(
        '<MEMRANGE INSTANCE="%s" BASEVALUE="0x%08X" HIGHVALUE="0x%08X" />'
        % item
        for item in ranges
    )
    hwh = (
        '<EDKSYSTEM VIVADOVERSION="2023.2"><MODULES>%s</MODULES>'
        '<MEMORYMAP>%s</MEMORYMAP></EDKSYSTEM>' % (module_xml, range_xml)
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xsa.json", json.dumps(metadata))
        archive.writestr("sysdef.xml", '<Project><SYSTEMINFO PART="%s" /></Project>' % part)
        archive.writestr("system.hwh", hwh)
        archive.writestr("system_top.bit", b"\xaa" * 1_000_000)
        if extra_member is not None:
            archive.writestr(extra_member, b"must not be extracted")
    return output.getvalue()


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


class ArtifactBoundaryTests(unittest.TestCase):
    def test_rootfs_paths_and_uimage_crc_are_fail_closed(self):
        archive = NewcArchive(
            (
                _entry(".", mode=0o040755),
                _entry("bin/tool", b"trusted", mode=0o100755, inode=2),
                _entry("sbin/tool", b"../bin/tool", mode=0o120777, inode=3),
            )
        )
        parsed = NewcArchive.parse(archive.to_bytes())
        self.assertEqual(parsed.read("/sbin/tool"), b"trusted")
        with self.assertRaises(FirmwareFormatError):
            NewcArchive((_entry("../escape"),))
        with self.assertRaisesRegex(FirmwareFormatError, "follows the first"):
            NewcArchive.parse(archive.to_bytes() + b"unexpected")

        image = _uimage(b"deterministic kernel payload")
        self.assertEqual(UImage(image).payload, b"deterministic kernel payload")
        corrupted = bytearray(image)
        corrupted[-1] ^= 1
        with self.assertRaisesRegex(FirmwareFormatError, "data CRC mismatch"):
            UImage(bytes(corrupted))

    def test_xsa_platform_contacts_pass_and_traversal_member_is_rejected(self):
        report = validate_xsa(_xsa())
        self.assertTrue(report.compatible, report.issues)
        self.assertEqual(report.facts["part"], "xc7z020clg400-1")
        self.assertEqual(report.facts["hardware_contacts"]["ddr_bus_width"], "16 Bit")
        self.assertEqual(
            report.facts["address_ranges"]["axi_ad9361_adc_dma"]["base"],
            0x7C400000,
        )
        unsafe = validate_xsa(_xsa("../escape"))
        self.assertFalse(unsafe.compatible)
        self.assertIn("xsa.members", {issue.check for issue in unsafe.issues})


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
