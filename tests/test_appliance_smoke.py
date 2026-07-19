"""Boot the twin end to end: deterministic snapshot, real NSFT TCP, full appliance."""

import contextlib
import io
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from neptunesdr_twin import NeptuneSDRTwin
from neptunesdr_twin.ad9361 import ENSMState
from neptunesdr_twin.cli import main
from neptunesdr_twin.fft import FFTConfig, ProcessingStatus
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder
from neptunesdr_twin.zynq import BootSource, BootStage


class ApplianceSmokeTests(unittest.TestCase):
    def test_composed_boot_is_deterministic_and_content_addressed(self):
        first = NeptuneSDRTwin()
        second = NeptuneSDRTwin()
        first.boot_to_userspace(BootSource.SD)
        second.boot_to_userspace(BootSource.SD)
        self.assertEqual(first.zynq.boot_stage, BootStage.RUNNING)
        self.assertEqual(first.radio.state, ENSMState.ALERT)
        self.assertEqual(first.snapshot(), second.snapshot())
        with tempfile.TemporaryDirectory() as directory:
            digest = first.write_snapshot(Path(directory) / "snapshot.json")
            self.assertEqual(len(digest), 64)

    def test_fft_result_can_cross_real_tcp_transport(self):
        twin = NeptuneSDRTwin()
        twin.configure_fft(
            FFTConfig(
                fft_size=256,
                channels=1,
                sample_rate_hz=256,
                update_rate_hz=1,
                window="rectangular",
                fftshift=False,
            )
        )
        address = twin.start_spectrum_publisher()
        client = socket.create_connection(address, timeout=1.0)
        client.settimeout(1.0)
        try:
            deadline = time.monotonic() + 1.0
            while twin._spectrum_publisher.client_count != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            result = twin.process_fft_frame([1 + 0j] * 256, timestamp_ns=11)
            self.assertEqual(result.status, ProcessingStatus.EMITTED)
            decoded = SpectrumStreamDecoder().feed(client.recv(4096))
            self.assertEqual(decoded[0].timestamp_ns, 11)
        finally:
            client.close()
            twin.close()

    def test_complete_appliance_can_bind_every_local_contact_and_stop(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                (
                    "appliance",
                    "--iiod-port",
                    "0",
                    "--spectrum-port",
                    "0",
                    "--usbip-port",
                    "0",
                    "--fft-size",
                    "256",
                    "--sample-rate",
                    "1024000",
                    "--bandwidth",
                    "1000000",
                    "--updates-per-second",
                    "4000",
                    "--no-default-tones",
                    "--duration",
                    "0",
                )
            )
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["fft_frames_per_update"], 1)
        self.assertEqual(result["endpoints"]["usbip_busid"], "1-1")
        for name in ("iiod", "spectrum", "usbip"):
            self.assertNotIn(":0", result["endpoints"][name])


if __name__ == "__main__":
    unittest.main()
