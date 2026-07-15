import importlib.util
import math
from pathlib import Path
import re
import socket
import threading
import unittest

from neptunesdr_twin.fft import PayloadEncoding, SpectrumPacket


ROOT = Path(__file__).resolve().parents[1]


def _capture_module():
    path = ROOT / "scripts/capture_guest_fft.py"
    spec = importlib.util.spec_from_file_location("capture_guest_fft", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GuestFFTFirmwareTests(unittest.TestCase):
    def test_guest_source_uses_iio_dmac_pl_fft_and_nsft_contacts(self):
        source = (ROOT / "firmware/neptune_fft_streamer.c").read_text()
        for contact in (
            'find_iio_device("cf-ad9361-lpc"',
            '"buffer/enable"',
            '"scan_elements/in_voltage%u_en"',
            "FFT_INPUT_PHYS             UINT32_C(0x18000000)",
            "FFT_OUTPUT_PHYS            UINT32_C(0x18100000)",
            "FFT_LOG2_N                 16U",
            "FFT_CONTROL_START",
            'memcpy(packet, "NSFT", 4)',
            "crc32_update",
            "STREAM_PORT                30432U",
            "IIO-DMAC then CPU-copy path",
            "input=iio-dmac-cpu-copy",
            "Pacing only: this delay is not a sustained-rate or 20 Hz claim.",
        ):
            self.assertIn(contact, source)

    def test_guest_register_map_matches_qemu_public_abi(self):
        guest = (ROOT / "firmware/neptune_fft_streamer.c").read_text()
        public = (
            ROOT / "cosim/qemu-10.0.2/include/hw/misc/p210_fft.h"
        ).read_text()
        offsets = {
            "ID": "0x000",
            "VERSION": "0x004",
            "CAPABILITIES": "0x008",
            "CONTROL": "0x00c",
            "STATUS": "0x010",
            "ERROR_CODE": "0x014",
            "LOG2_N": "0x018",
            "CHANNEL_COUNT": "0x01c",
            "CHANNEL_MASK": "0x020",
            "INPUT_ADDR": "0x024",
            "INPUT_BYTES": "0x028",
            "OUTPUT_ADDR": "0x02c",
            "OUTPUT_BYTES": "0x030",
            "SEQUENCE": "0x034",
            "RESULT_SEQUENCE": "0x038",
            "BINS_WRITTEN": "0x04c",
        }
        guest_defines = dict(
            re.findall(r"^#define FFT_REG_(\w+)\s+(0x[0-9a-f]+)U$", guest, re.M)
        )
        public_defines = dict(
            re.findall(r"^#define P210_FFT_REG_(\w+)\s+(0x[0-9a-f]+)$", public, re.M)
        )
        for name, offset in offsets.items():
            self.assertEqual(guest_defines[name], offset)
            self.assertEqual(public_defines[name], offset)
        self.assertIn("FFT_ID                     UINT32_C(0x5446464e)", guest)
        self.assertIn("P210_FFT_ID                         0x5446464eU", public)

    def test_guest_rejects_incompatible_fft_capabilities_and_size_range(self):
        source = (ROOT / "firmware/neptune_fft_streamer.c").read_text()
        for required_capability in (
            "FFT_CAP_IQ16_LE",
            "FFT_CAP_POWER_U32_LE",
            "FFT_CAP_TWO_CHANNEL",
            "FFT_CAP_SCALE_EACH_STAGE",
            "FFT_CAP_NATURAL_ORDER",
        ):
            self.assertIn(required_capability, source)
        self.assertIn(
            "(capabilities & FFT_CAPABILITIES_REQUIRED) !=",
            source,
        )
        self.assertIn("min_log2_n > FFT_LOG2_N", source)
        self.assertIn("max_log2_n < FFT_LOG2_N", source)

    def test_guest_dbfs_reference_is_signed_12_bit_not_int16_container(self):
        source = (ROOT / "firmware/neptune_fft_streamer.c").read_text()
        self.assertIn("AD9361_ADC_FULL_SCALE       2048.0", source)
        self.assertNotIn("32768.0 * 32768.0", source)
        self.assertAlmostEqual(20.0 * math.log10(1536.0 / 2048.0), -2.4988, places=4)
        self.assertAlmostEqual(20.0 * math.log10(1024.0 / 2048.0), -6.0206, places=4)

    def test_guest_logs_transmission_only_after_both_socket_sends_succeed(self):
        source = (ROOT / "firmware/neptune_fft_streamer.c").read_text()
        send_check = source.index("if (send_all_socket(client, packet, count) != 0)")
        failure_break = source.index("if (send_error)", send_check)
        transmitted = source.index("NEPTUNE_FFT transmitted sequence=", failure_break)
        self.assertLess(send_check, failure_break)
        self.assertLess(failure_break, transmitted)
        self.assertIn("static int send_all_socket", source)
        self.assertIn("errno = EPIPE;", source)
        self.assertIn("shutdown(client, SHUT_RDWR);", source)
        self.assertIn("socket_peer_closed(client)", source)

    def test_host_capture_crc_checks_a_two_channel_update(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        values0 = [-200.0] * 65_536
        values1 = [-200.0] * 65_536
        values0[5_120] = -2.53
        values1[13_312] = -6.08
        packets = [
            SpectrumPacket(
                sequence=7,
                channel=channel,
                fft_size=65_536,
                sample_rate_hz=61_440_000,
                center_frequency_hz=2_400_000_000,
                timestamp_ns=123,
                config_epoch=0,
                bin_start=0,
                values_dbfs=tuple(values),
                encoding=PayloadEncoding.UINT16_LOG_POWER,
            ).pack()
            for channel, values in enumerate((values0, values1))
        ]

        def serve():
            client, _ = listener.accept()
            with client:
                client.sendall(packets[0][:37])
                client.sendall(packets[0][37:] + packets[1])
            listener.close()

        thread = threading.Thread(target=serve)
        thread.start()
        module = _capture_module()
        report = module.capture_update(address[0], address[1], 2.0)
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["crc_checked"])
        self.assertEqual(
            [channel["peak_bin"] for channel in report["channels"]],
            [5_120, 13_312],
        )


if __name__ == "__main__":
    unittest.main()
