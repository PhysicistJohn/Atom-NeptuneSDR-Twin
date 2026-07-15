import io
import json
import unittest
import zipfile

from neptunesdr_twin.xsa import validate_xsa


def build_xsa(part="xc7z020clg400-1", include_dma=True):
    metadata = {
        "hardware": "true",
        "generatedVersion": "2023.2",
        "generatedTimestamp": "test",
        "devices": [{"part": {"name": part}}],
    }
    modules = [
        ("sys_ps7", "xilinx.com:ip:processing_system7:5.5"),
        ("axi_ad9361", "analog.com:user:axi_ad9361:1.0"),
        ("cpack", "analog.com:user:util_cpack2:1.0"),
        ("tx_upack", "analog.com:user:util_upack2:1.0"),
    ]
    if include_dma:
        modules.extend(
            [
                ("axi_ad9361_adc_dma", "analog.com:user:axi_dmac:1.0"),
                ("axi_ad9361_dac_dma", "analog.com:user:axi_dmac:1.0"),
            ]
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
    module_xml = ""
    for instance, vlnv in modules:
        parameter_xml = "".join(
            '<PARAMETER NAME="%s" VALUE="%s" />' % item
            for item in parameters.get(instance, {}).items()
        )
        module_xml += '<MODULE INSTANCE="%s" VLNV="%s">%s</MODULE>' % (
            instance,
            vlnv,
            parameter_xml,
        )
    ranges = {
        "axi_ad9361": (0x79020000, 0x7902FFFF),
        "axi_ad9361_adc_dma": (0x7C400000, 0x7C400FFF),
        "axi_ad9361_dac_dma": (0x7C420000, 0x7C420FFF),
    }
    range_xml = "".join(
        '<MEMRANGE INSTANCE="%s" BASEVALUE="0x%08X" HIGHVALUE="0x%08X" />'
        % (name, base, high)
        for name, (base, high) in ranges.items()
    )
    hwh = (
        '<EDKSYSTEM VIVADOVERSION="2023.2"><MODULES>%s</MODULES>'
        '<MEMORYMAP>%s</MEMORYMAP></EDKSYSTEM>' % (module_xml, range_xml)
    )
    sysdef = '<Project><SYSTEMINFO PART="%s" /></Project>' % part
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xsa.json", json.dumps(metadata))
        archive.writestr("sysdef.xml", sysdef)
        archive.writestr("system.hwh", hwh)
        archive.writestr("system_top.bit", b"\xaa" * 1_000_000)
    return output.getvalue()


class XSAValidationTests(unittest.TestCase):
    def test_valid_platform_exposes_part_modules_and_register_bases(self):
        report = validate_xsa(build_xsa())
        self.assertTrue(report.compatible, report.issues)
        self.assertEqual(report.facts["part"], "xc7z020clg400-1")
        self.assertEqual(
            report.facts["address_ranges"]["axi_ad9361_adc_dma"]["base"],
            0x7C400000,
        )
        self.assertEqual(report.facts["hardware_contacts"]["ddr_bus_width"], "16 Bit")
        self.assertEqual(report.facts["hardware_contacts"]["iq_container_bits"], "16")
        self.assertIn("not executed by QEMU", report.facts["qemu_boundary"])

    def test_wrong_part_and_missing_dma_fail_closed(self):
        report = validate_xsa(build_xsa(part="xc7z010clg400-1", include_dma=False))
        self.assertFalse(report.compatible)
        checks = {issue.check for issue in report.issues}
        self.assertIn("xsa.part", checks)
        self.assertIn("xsa.module.axi_ad9361_adc_dma", checks)

    def test_missing_member_and_non_zip_are_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("xsa.json", "{}")
        self.assertFalse(validate_xsa(output.getvalue()).compatible)
        self.assertFalse(validate_xsa(b"not a zip").compatible)


if __name__ == "__main__":
    unittest.main()
