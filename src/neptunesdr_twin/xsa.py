"""Fail-closed inspection of the pinned P210 Xilinx hardware handoff.

An XSA is evidence about the PL/PS hardware platform, not a machine image that
QEMU can execute.  This validator therefore checks the part, bitstream,
declared IP, and address contacts without pretending to simulate encrypted or
vendor-specific FPGA configuration semantics.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Union
import xml.etree.ElementTree as ET
import zipfile

from .firmware import FirmwareReport


REQUIRED_XSA_MEMBERS = (
    "system_top.bit",
    "sysdef.xml",
    "system.hwh",
    "xsa.json",
)
EXPECTED_PART = "xc7z020clg400-1"
EXPECTED_MODULES: Mapping[str, str] = {
    "sys_ps7": "xilinx.com:ip:processing_system7:",
    "axi_ad9361": "analog.com:user:axi_ad9361:",
    "axi_ad9361_adc_dma": "analog.com:user:axi_dmac:",
    "axi_ad9361_dac_dma": "analog.com:user:axi_dmac:",
    "cpack": "analog.com:user:util_cpack2:",
    "tx_upack": "analog.com:user:util_upack2:",
}
EXPECTED_BASES: Mapping[str, int] = {
    "axi_ad9361": 0x79020000,
    "axi_ad9361_adc_dma": 0x7C400000,
    "axi_ad9361_dac_dma": 0x7C420000,
}
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def _read_xsa(source: Union[Path, str, bytes, bytearray]) -> tuple:
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        label = "XSA bytes"
    else:
        path = Path(source)
        raw = path.read_bytes()
        label = str(path)
    return raw, label


def validate_xsa(source: Union[Path, str, bytes, bytearray]) -> FirmwareReport:
    """Validate a Neptune/P210 hardware-platform XSA and extract ABI facts."""

    try:
        raw, label = _read_xsa(source)
    except OSError as exc:
        report = FirmwareReport(str(source))
        report.add("error", "xsa.read", str(exc))
        return report

    report = FirmwareReport(label)
    report.hashes[Path(label).name] = hashlib.sha256(raw).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        report.add("error", "xsa.zip", "not a readable XSA ZIP: %s" % exc)
        return report

    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            report.add("error", "xsa.members", "archive contains duplicate member names")
        for info in infos:
            if not _safe_member_name(info.filename):
                report.add("error", "xsa.members", "unsafe member name %r" % info.filename)
            if info.flag_bits & 0x1:
                report.add("error", "xsa.members", "encrypted member %r is unsupported" % info.filename)
            if info.file_size > MAX_MEMBER_BYTES:
                report.add("error", "xsa.members", "member %r exceeds the size limit" % info.filename)
        if sum(item.file_size for item in infos) > MAX_TOTAL_BYTES:
            report.add("error", "xsa.members", "expanded XSA exceeds the size limit")

        missing = sorted(set(REQUIRED_XSA_MEMBERS) - set(names))
        if missing:
            report.add("error", "xsa.members", "missing required files: " + ", ".join(missing))
            return report
        if not report.compatible:
            return report

        members: Dict[str, bytes] = {}
        try:
            for name in REQUIRED_XSA_MEMBERS:
                members[name] = archive.read(name)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            report.add("error", "xsa.members", "cannot read required member: %s" % exc)
            return report
        for name, data in sorted(members.items()):
            report.hashes[name] = hashlib.sha256(data).hexdigest()

    try:
        metadata = json.loads(members["xsa.json"].decode("utf-8"))
        devices = metadata.get("devices", [])
        part = devices[0].get("part", {}).get("name") if devices else None
        report.facts["part"] = part
        report.facts["vivado_version"] = metadata.get("generatedVersion")
        report.facts["generated_timestamp"] = metadata.get("generatedTimestamp")
        report.facts["bitstream_bytes"] = len(members["system_top.bit"])
        report.facts["hardware"] = metadata.get("hardware")
        if part != EXPECTED_PART:
            report.add("error", "xsa.part", "expected %s, found %r" % (EXPECTED_PART, part))
        if str(metadata.get("hardware")).lower() != "true":
            report.add("error", "xsa.kind", "XSA does not declare a hardware platform")
        if len(members["system_top.bit"]) < 1_000_000:
            report.add("error", "xsa.bitstream", "system_top.bit is implausibly small")
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
        report.add("error", "xsa.metadata", "invalid xsa.json: %s" % exc)

    try:
        sysdef = ET.fromstring(members["sysdef.xml"])
        system_info = sysdef.find(".//SYSTEMINFO")
        sysdef_part = system_info.get("PART") if system_info is not None else None
        report.facts["sysdef_part"] = sysdef_part
        if sysdef_part != EXPECTED_PART:
            report.add(
                "error",
                "xsa.sysdef.part",
                "expected %s, found %r" % (EXPECTED_PART, sysdef_part),
            )
    except ET.ParseError as exc:
        report.add("error", "xsa.sysdef", "invalid sysdef.xml: %s" % exc)

    try:
        hwh = ET.fromstring(members["system.hwh"])
        modules = {
            element.get("INSTANCE"): element.get("VLNV")
            for element in hwh.iter("MODULE")
            if element.get("INSTANCE") and element.get("VLNV")
        }
        report.facts["hwh_vivado_version"] = hwh.get("VIVADOVERSION")
        report.facts["modules"] = dict(sorted(modules.items()))
        for instance, prefix in EXPECTED_MODULES.items():
            actual = modules.get(instance, "")
            if not actual.startswith(prefix):
                report.add(
                    "error",
                    "xsa.module." + instance,
                    "expected VLNV prefix %s, found %r" % (prefix, actual),
                )

        module_elements = {
            element.get("INSTANCE"): element
            for element in hwh.iter("MODULE")
            if element.get("INSTANCE")
        }

        def parameters(instance: str) -> Dict[str, str]:
            module = module_elements.get(instance)
            if module is None:
                return {}
            return {
                element.get("NAME"): element.get("VALUE", "")
                for element in module.iter("PARAMETER")
                if element.get("NAME")
            }

        ps_parameters = parameters("sys_ps7")
        cpack_parameters = parameters("cpack")
        adc_dma_parameters = parameters("axi_ad9361_adc_dma")
        hardware_contacts = {
            "cpu_frequency_mhz": ps_parameters.get("PCW_ACT_APU_PERIPHERAL_FREQMHZ"),
            "ddr_bus_width": ps_parameters.get("PCW_UIPARAM_DDR_BUS_WIDTH"),
            "ddr_frequency_mhz": ps_parameters.get("PCW_UIPARAM_ACT_DDR_FREQ_MHZ"),
            "ddr_part": ps_parameters.get("PCW_UIPARAM_DDR_PARTNO"),
            "fclk0_hz": ps_parameters.get("PCW_CLK0_FREQ"),
            "iq_scalar_lanes": cpack_parameters.get("NUM_OF_CHANNELS"),
            "iq_container_bits": cpack_parameters.get("SAMPLE_DATA_WIDTH"),
            "iq_samples_per_lane_per_clock": cpack_parameters.get("SAMPLES_PER_CHANNEL"),
            "rx_dma_source_bits": adc_dma_parameters.get("DMA_DATA_WIDTH_SRC"),
            "rx_dma_destination_bits": adc_dma_parameters.get("DMA_DATA_WIDTH_DEST"),
        }
        report.facts["hardware_contacts"] = hardware_contacts
        expected_contacts = {
            "ddr_bus_width": "16 Bit",
            "fclk0_hz": "100000000",
            "iq_scalar_lanes": "4",
            "iq_container_bits": "16",
            "iq_samples_per_lane_per_clock": "1",
            "rx_dma_source_bits": "64",
            "rx_dma_destination_bits": "64",
        }
        for name, expected in expected_contacts.items():
            if hardware_contacts.get(name) != expected:
                report.add(
                    "error",
                    "xsa.contact." + name,
                    "expected %r, found %r" % (expected, hardware_contacts.get(name)),
                )

        address_ranges: Dict[str, Dict[str, int]] = {}
        for element in hwh.iter("MEMRANGE"):
            instance = element.get("INSTANCE")
            base = element.get("BASEVALUE")
            high = element.get("HIGHVALUE")
            if not instance or not base or not high or instance in address_ranges:
                continue
            address_ranges[instance] = {"base": int(base, 0), "high": int(high, 0)}
        report.facts["address_ranges"] = dict(sorted(address_ranges.items()))
        for instance, expected in EXPECTED_BASES.items():
            actual_range = address_ranges.get(instance)
            actual = actual_range["base"] if actual_range else None
            if actual != expected:
                report.add(
                    "error",
                    "xsa.address." + instance,
                    "expected base 0x%08x, found %r" % (expected, actual),
                )
    except (ET.ParseError, ValueError) as exc:
        report.add("error", "xsa.hwh", "invalid system.hwh: %s" % exc)

    report.facts["qemu_boundary"] = (
        "XSA bitstreams/HWH are validated as hardware evidence; they are not executed by QEMU"
    )
    return report


__all__ = [
    "EXPECTED_BASES",
    "EXPECTED_MODULES",
    "EXPECTED_PART",
    "REQUIRED_XSA_MEMBERS",
    "validate_xsa",
]
