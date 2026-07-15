import tempfile
import unittest
from pathlib import Path
import socket
import threading
import time

from neptunesdr_twin import NeptuneSDRTwin
from neptunesdr_twin.ad9361 import ENSMState
from neptunesdr_twin.fft import FFTConfig, ProcessingStatus
from neptunesdr_twin.iio import IIODSession
from neptunesdr_twin.spectrum_transport import SpectrumStreamDecoder
from neptunesdr_twin.zynq import BootSource, BootStage


class BoardTests(unittest.TestCase):
    @staticmethod
    def _configure_small_continuous_twin(twin):
        twin.boot_to_userspace()
        twin.radio.set_rf_bandwidth("rx", 1_000_000)
        twin.radio.set_rf_bandwidth("tx", 1_000_000)
        twin.radio.set_sample_rate(1_024_000)
        twin.radio.set_ensm_state(ENSMState.FDD)
        twin.configure_fft(
            FFTConfig(
                fft_size=256,
                channels=2,
                sample_rate_hz=1_024_000,
                update_rate_hz=4_000,
                window="rectangular",
                fftshift=False,
            )
        )

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

    def test_continuous_pl_runtime_is_composed_and_publishes_atomic_pair(self):
        twin = NeptuneSDRTwin()
        self._configure_small_continuous_twin(twin)
        twin.rf.add_baseband_tone(0, 128_000, amplitude=1_000)
        twin.rf.add_baseband_tone(1, 256_000, amplitude=500)
        address = twin.start_spectrum_publisher()
        client = socket.create_connection(address, timeout=1.0)
        client.settimeout(2.0)
        try:
            deadline = time.monotonic() + 1.0
            while twin._spectrum_publisher.client_count != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            runtime = twin.start_continuous_spectrum(realtime_pacing=False)
            decoder = SpectrumStreamDecoder()
            packets = ()
            deadline = time.monotonic() + 2.0
            while len(packets) < 2 and time.monotonic() < deadline:
                packets += decoder.feed(client.recv(65536))
            self.assertEqual(tuple(packet.channel for packet in packets[:2]), (0, 1))
            self.assertEqual(packets[0].sequence, packets[1].sequence)
            self.assertEqual(packets[0].sample_rate_hz, 1_024_000)
            self.assertGreaterEqual(runtime.counters.updates_published, 1)
            self.assertTrue(twin.snapshot()["continuous_pl_spectrum"]["running"])
        finally:
            client.close()
            twin.close()
        self.assertIsNone(twin.continuous_spectrum)

    def test_continuous_runtime_exclusively_owns_raw_rx_and_spectrum_publish(self):
        twin = NeptuneSDRTwin()
        self._configure_small_continuous_twin(twin)
        session = IIODSession(twin.iio)
        self.assertEqual(
            session.execute(b"OPEN iio:device3 8 0000000f\r\n"), b"0\n"
        )
        runtime = twin.start_continuous_spectrum(
            publisher=lambda pair: True,
            realtime_pacing=False,
        )
        self.assertTrue(runtime.wait_configured(1.0))
        self.assertEqual(
            session.execute(b"READBUF iio:device3 8\r\n"), b"-1\n"
        )
        with self.assertRaisesRegex(RuntimeError, "manual FFT processing"):
            twin.process_fft_frame(
                ([0j] * 256, [0j] * 256),
                timestamp_ns=0,
            )
        self.assertTrue(twin.stop_continuous_spectrum())
        self.assertTrue(session.execute(b"READBUF iio:device3 8\r\n").startswith(b"8\n"))
        twin.close()

    def test_power_off_never_resets_shared_state_under_a_stuck_pl_callback(self):
        twin = NeptuneSDRTwin()
        self._configure_small_continuous_twin(twin)
        callback_entered = threading.Event()
        callback_release = threading.Event()

        def blocking_publisher(pair):
            callback_entered.set()
            callback_release.wait(2.0)
            return True

        twin.start_continuous_spectrum(
            publisher=blocking_publisher,
            realtime_pacing=False,
        )
        self.assertTrue(callback_entered.wait(1.0))
        with self.assertRaisesRegex(RuntimeError, "did not stop"):
            twin.power_off(runtime_timeout_s=0.01)
        self.assertEqual(twin.zynq.boot_stage, BootStage.RUNNING)
        self.assertIsNotNone(twin.continuous_spectrum)

        callback_release.set()
        twin.power_off(runtime_timeout_s=1.0)
        self.assertEqual(twin.zynq.boot_stage, BootStage.OFF)
        self.assertIsNone(twin.continuous_spectrum)

    def test_pl_start_waits_for_an_inflight_iio_read_before_claiming_rf(self):
        twin = NeptuneSDRTwin()
        self._configure_small_continuous_twin(twin)
        original_read = twin.rf.stream_rx_bytes
        read_entered = threading.Event()
        read_release = threading.Event()

        def blocking_read(length):
            read_entered.set()
            read_release.wait(1.0)
            return original_read(length)

        twin.rf.stream_rx_bytes = blocking_read
        read_result = []
        read_thread = threading.Thread(
            target=lambda: read_result.append(twin._provide_rx(8))
        )
        read_thread.start()
        self.assertTrue(read_entered.wait(1.0))

        runtime_result = []
        start_thread = threading.Thread(
            target=lambda: runtime_result.append(
                twin.start_continuous_spectrum(
                    publisher=lambda pair: True,
                    realtime_pacing=False,
                )
            )
        )
        start_thread.start()
        time.sleep(0.02)
        self.assertTrue(start_thread.is_alive())

        read_release.set()
        read_thread.join(1.0)
        start_thread.join(1.0)
        self.assertFalse(read_thread.is_alive())
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(len(read_result[0]), 8)
        self.assertEqual(len(runtime_result), 1)
        self.assertTrue(runtime_result[0].wait_configured(1.0))
        twin.close()

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
