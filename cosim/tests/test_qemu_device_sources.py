"""Regression checks for the QEMU P210 device sources.

The compile test deliberately reuses compile_commands.json from the pinned
QEMU build.  That catches QEMU API changes and warnings which a standalone C
compiler invocation would miss.  It is skipped when the optional QEMU source
build is not present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
COSIM = ROOT / "cosim"
QEMU_SOURCES = COSIM / "qemu-10.0.2"


def _manifest() -> dict:
    return json.loads((COSIM / "p210-contacts.json").read_text())


def _qemu_build_dir() -> Path:
    override = os.environ.get("P210_QEMU_BUILD_DIR")
    if override:
        return Path(override)
    return ROOT / ".cache/qemu-p210/src/qemu-10.0.2/build-p210"


def _compile_with_qemu_command(
    database: list[dict], template_suffix: str, source: Path, output: Path
) -> None:
    entry = next(item for item in database if item["file"].endswith(template_suffix))
    command = shlex.split(entry["command"])
    original_output = entry["output"]

    command = [str(source) if arg == entry["file"] else arg for arg in command]
    command = [
        str(output) if arg == original_output else
        str(output) + ".d" if arg == original_output + ".d" else arg
        for arg in command
    ]
    command.insert(1, "-I" + str(QEMU_SOURCES / "include"))
    result = subprocess.run(
        command,
        cwd=entry["directory"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout)


class QEMUDeviceSourceTests(unittest.TestCase):
    def test_contact_manifest_names_real_sources(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest["target"]["qemu_release"], "10.0.2")
        for relative in manifest["sources"]:
            self.assertTrue((COSIM / relative).is_file(), relative)

    def test_probe_contacts_are_present_in_device_sources(self) -> None:
        pl = (QEMU_SOURCES / "hw/misc/p210_sdr.c").read_text()
        spi = (QEMU_SOURCES / "hw/ssi/p210_ad9361.c").read_text()

        for contact in (
            "#define P210_RX_CORE_VERSION            0x000a0061",
            "#define P210_TX_CORE_VERSION            0x00090061",
            "#define P210_DMAC_VERSION               0x00040061",
            "#define DMAC_X_LENGTH_MASK              0x00ffffff",
            "#define P210_DMAC_QUEUE_DEPTH            4",
            "#define P210_DRP_LOCKED                 BIT(17)",
            "#define P210_STATUS_VALID               BIT(0)",
            "#define P210_RX_TONE_PHASES             64",
            "#define P210_RX_TONE0_STEP              5",
            "#define P210_RX_TONE1_STEP              13",
            "static size_t p210_rx_fill",
            "uint64_t rx_sample_index",
            "static bool p210_dmac_accept_descriptor",
            "dmac->queue_count == P210_DMAC_QUEUE_DEPTH",
            "dmac->regs[DMAC_REG_TRANSFER_DONE / 4] &= ~BIT(id)",
            "VMSTATE_UINT64(rx_sample_index, P210SDRState)",
            "DEFINE_PROP_UINT16(\"rx-tone0-amplitude\"",
            "DEFINE_PROP_UINT8(\"rx-tone1-step\"",
            "P210_REG_CHAN_STATUS(15)",
        ):
            self.assertIn(contact, pl)

        for contact in (
            "#define AD9361_REG_PRODUCT_ID            0x037",
            "#define AD9361_PRODUCT_ID                0x0a",
            "#define AD9361_REG_RX_BBF_R2346          0x1e6",
            "#define AD9361_REG_RX_BBF_C3_MSB         0x1eb",
            "#define AD9361_REG_RX_BBF_C3_LSB         0x1ec",
            "#define AD9361_REG_RX_CAL_STATUS         0x244",
            "#define AD9361_REG_TX_CAL_STATUS         0x284",
            "#define AD9361_VCO_LOCK                  BIT(1)",
            "p210_ad9361_complete_rx_bbf_cal(s)",
            "s->regs[AD9361_REG_RX_BBF_C3_LSB] = 0x36",
            "s->address - s->data_index",
        ):
            self.assertIn(contact, spi)

        calibration = _manifest()["ad9361_spi"]["rx_bbf_calibration"]
        self.assertEqual(calibration["p210_startup_bbpll_hz"], 983040000)
        self.assertEqual(calibration["p210_startup_tune_divide"], 9)
        self.assertEqual(
            calibration["p210_startup_outputs"],
            {"0x1e6": "0x01", "0x1eb": "0x00", "0x1ec": "0x36"},
        )

        dmac = _manifest()["axi_dmac_probe"]
        self.assertEqual(dmac["dma_length_width"], 24)
        self.assertEqual(dmac["x_length_write_mask"], "0x00ffffff")
        self.assertEqual(dmac["maximum_transfer_segment_bytes"], 1 << 24)
        self.assertEqual(dmac["descriptor_queue_depth"], 4)
        self.assertEqual(dmac["transfer_id_modulus"], 4)
        self.assertEqual(dmac["transfer_done_mask"], "0x0000000f")
        payload = dmac["rx_payload"]
        self.assertEqual(
            payload["scan_order"], ["RX1_I", "RX1_Q", "RX2_I", "RX2_Q"]
        )
        self.assertEqual(payload["tone_period_samples"], 64)
        self.assertEqual(payload["rx1"]["phase_step"], 5)
        self.assertEqual(payload["rx2"]["phase_step"], 13)

        fft = _manifest()["fft_accelerator"]
        self.assertEqual(fft["base"], "0x7c450000")
        self.assertEqual(fft["gic_spi"], 58)
        self.assertEqual(fft["maximum_fft_size"], 65536)

    def test_sources_compile_with_qemu_10_flags(self) -> None:
        build = _qemu_build_dir()
        commands = build / "compile_commands.json"
        if not commands.is_file():
            self.skipTest(
                "set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build"
            )

        database = json.loads(commands.read_text())
        with tempfile.TemporaryDirectory(prefix="p210-qemu-compile-") as tmp:
            out = Path(tmp)
            _compile_with_qemu_command(
                database,
                "hw/misc/applesmc.c",
                QEMU_SOURCES / "hw/misc/p210_sdr.c",
                out / "p210_sdr.o",
            )
            _compile_with_qemu_command(
                database,
                "hw/ssi/ssi.c",
                QEMU_SOURCES / "hw/ssi/p210_ad9361.c",
                out / "p210_ad9361.o",
            )


if __name__ == "__main__":
    unittest.main()
