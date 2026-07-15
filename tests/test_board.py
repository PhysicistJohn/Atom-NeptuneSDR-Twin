import tempfile
import unittest
from pathlib import Path
import socket
import time

from neptunesdr_twin import NeptuneSDRTwin
from neptunesdr_twin.ad9361 import ENSMState
from neptunesdr_twin.fft import FFTConfig, ProcessingStatus
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder
from neptunesdr_twin.zynq import BootSource, BootStage


class BoardTests(unittest.TestCase):
    def test_fft_pipeline_is_part_of_snapshot_and_can_emit_packets(self):
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
        result = twin.fft.process_frame([1 + 0j] * 256, timestamp_ns=7)
        self.assertEqual(result.status, ProcessingStatus.EMITTED)
        self.assertEqual(result.packets[0].timestamp_ns, 7)
        snapshot = twin.snapshot()
        self.assertEqual(snapshot["fft"]["fft_size"], 256)
        self.assertTrue(snapshot["fft"]["ingress"]["fits"])

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

    def test_composed_boot_is_deterministic(self):
        first = NeptuneSDRTwin()
        second = NeptuneSDRTwin()
        first.boot_to_userspace(BootSource.SD)
        second.boot_to_userspace(BootSource.SD)
        self.assertEqual(first.zynq.boot_stage, BootStage.RUNNING)
        self.assertEqual(first.radio.state, ENSMState.ALERT)
        self.assertEqual(first.snapshot(), second.snapshot())

    def test_snapshot_is_content_addressed(self):
        twin = NeptuneSDRTwin()
        twin.boot_to_userspace()
        with tempfile.TemporaryDirectory() as directory:
            digest = twin.write_snapshot(Path(directory) / "snapshot.json")
            self.assertEqual(len(digest), 64)

    def test_iio_tx_reaches_composed_rf_fifo(self):
        twin = NeptuneSDRTwin()
        twin._consume_tx(b"\x01\0\x02\0\x03\0\x04\0")
        self.assertEqual(twin.rf.tx_fifo.stats.pushed_frames, 1)

    def test_usb_descriptor_and_iio_are_in_same_composition(self):
        twin = NeptuneSDRTwin(serial="unit-test")
        self.assertEqual(twin.usb.profile.parsed_device.product_id, 0xB673)
        self.assertEqual(twin.iio.serial, "unit-test")


if __name__ == "__main__":
    unittest.main()
