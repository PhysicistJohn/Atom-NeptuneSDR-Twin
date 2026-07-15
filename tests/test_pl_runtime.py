import math
import threading
import time
import unittest

from neptunesdr_twin.ad9361 import AD9361, ENSMState
from neptunesdr_twin.fft import (
    FFTConfig,
    PacketFlag,
    PayloadEncoding,
    SpectrumPacket,
)
from neptunesdr_twin.pl_runtime import (
    ContinuousPLSpectrumRuntime,
    PLRuntimeContinuityError,
    PLRuntimeError,
    PLStepStatus,
)
from neptunesdr_twin.rf import RFModel


def active_radio(sample_rate_hz=1_024_000, bandwidth_hz=1_024_000):
    radio = AD9361()
    radio.set_rf_bandwidth("rx", bandwidth_hz)
    radio.set_rf_bandwidth("tx", bandwidth_hz)
    radio.set_sample_rate(sample_rate_hz)
    radio.set_ensm_state(ENSMState.ALERT)
    radio.set_ensm_state(ENSMState.FDD)
    return radio


def small_config(*, update_rate_hz=2_000.0):
    return FFTConfig(
        fft_size=256,
        channels=2,
        window="rectangular",
        coherent_gain_normalization=True,
        fftshift=False,
        averages=1,
        update_rate_hz=update_rate_hz,
        sample_rate_hz=61_440_000,  # replaced atomically from the live radio
        center_frequency_hz=1,
        payload_encoding=PayloadEncoding.FLOAT32_DBFS,
        full_scale=1_000.0,
    )


class DeterministicPLRuntimeTests(unittest.TestCase):
    def test_runtime_contract_types_are_exported_from_the_package(self):
        import neptunesdr_twin

        for name in (
            "PacketPair",
            "PLRuntimeContinuityError",
            "PLRuntimeCounters",
            "PLRuntimeError",
            "PLStepResult",
            "PLStepStatus",
        ):
            self.assertTrue(hasattr(neptunesdr_twin, name), name)

    def test_consecutive_2x2_blocks_average_and_publish_one_atomic_pair(self):
        radio = active_radio()
        rf = RFModel(radio)
        rf.add_baseband_tone(0, radio.sample_rate_hz / 8, amplitude=1_000)
        rf.add_baseband_tone(1, -radio.sample_rate_hz / 4, amplitude=500)
        received = []

        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=2_000),
            lambda pair: received.append(pair),
            realtime_pacing=False,
        )
        first = runtime.step()
        second = runtime.step()

        self.assertEqual(first.status, PLStepStatus.ACCUMULATING)
        self.assertEqual(second.status, PLStepStatus.PUBLISHED)
        self.assertEqual(len(received), 1)
        pair = received[0]
        self.assertEqual(tuple(packet.channel for packet in pair), (0, 1))
        self.assertEqual(pair[0].sequence, pair[1].sequence)
        self.assertEqual(pair[0].timestamp_ns, 0)
        self.assertEqual(pair[0].config_epoch, radio.config_epoch)
        self.assertEqual(pair[0].sample_rate_hz, radio.sample_rate_hz)
        self.assertEqual(pair[0].center_frequency_hz, radio.rx_lo_hz)
        self.assertEqual(max(range(256), key=pair[0].values_dbfs.__getitem__), 32)
        self.assertEqual(max(range(256), key=pair[1].values_dbfs.__getitem__), 192)
        # RFModel quantises the sinusoid to signed converter counts before the
        # FFT, so a sub-millidecibel residual is expected.
        self.assertAlmostEqual(pair[0].values_dbfs[32], 0.0, delta=0.002)
        self.assertAlmostEqual(
            pair[1].values_dbfs[192], 20.0 * math.log10(0.5), places=8
        )
        self.assertEqual(second.counters.iq_frames_consumed, 512)
        self.assertEqual(second.counters.fft_frames_processed, 2)
        self.assertEqual(second.counters.updates_generated, 1)
        self.assertEqual(second.counters.updates_published, 1)
        self.assertEqual(second.counters.dropped_updates, 0)
        for packet in pair:
            decoded = SpectrumPacket.unpack(packet.pack())
            self.assertEqual(decoded.sequence, packet.sequence)
            self.assertEqual(decoded.channel, packet.channel)
            self.assertEqual(decoded.config_epoch, packet.config_epoch)
            self.assertEqual(decoded.bin_count, packet.bin_count)

    def test_retune_never_mixes_epochs_and_reports_truncated_average(self):
        radio = active_radio()
        rf = RFModel(radio)
        published = []
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=2_000),
            lambda pair: published.append(pair),
            realtime_pacing=False,
        )

        old_epoch = radio.config_epoch
        old_center = radio.rx_lo_hz
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        radio.set_lo_frequency("rx", old_center + 1_000_000)
        new_epoch = radio.config_epoch
        self.assertNotEqual(old_epoch, new_epoch)
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        emitted = runtime.step()

        self.assertEqual(emitted.status, PLStepStatus.PUBLISHED)
        self.assertEqual(len(published), 1)
        pair = published[0]
        for packet in pair:
            self.assertEqual(packet.config_epoch, new_epoch)
            self.assertEqual(packet.center_frequency_hz, old_center + 1_000_000)
            self.assertEqual(packet.dropped_frames, 1)
            self.assertEqual(packet.overrun_events, 1)
            decoded = SpectrumPacket.unpack(packet.pack())
            self.assertTrue(decoded.flags & PacketFlag.DROPPED_FRAMES)
            self.assertTrue(decoded.flags & PacketFlag.INPUT_OVERRUN)
        counters = runtime.counters
        self.assertEqual(counters.configuration_activations, 2)
        self.assertEqual(counters.reconfigurations, 1)
        self.assertEqual(counters.reconfiguration_discarded_fft_frames, 1)
        self.assertEqual(counters.reconfiguration_discarded_iq_frames, 256)
        self.assertEqual(counters.dropped_updates, 0)

    def test_bounded_backpressure_stalls_before_consuming_or_dropping(self):
        radio = active_radio()
        rf = RFModel(radio)
        ready = [False]
        accepted_sequences = []

        def publisher(pair):
            if not ready[0]:
                return False
            accepted_sequences.append(pair[0].sequence)
            return True

        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            publisher,
            pending_update_capacity=1,
            realtime_pacing=False,
        )
        queued = runtime.step()
        consumed = runtime.counters.iq_frames_consumed
        blocked = runtime.step()

        self.assertEqual(queued.status, PLStepStatus.QUEUED)
        self.assertEqual(blocked.status, PLStepStatus.BACKPRESSURED)
        self.assertEqual(runtime.pending_updates, 1)
        self.assertEqual(runtime.counters.iq_frames_consumed, consumed)
        self.assertEqual(runtime.counters.updates_generated, 1)
        self.assertEqual(runtime.counters.dropped_updates, 0)
        self.assertGreaterEqual(runtime.counters.publisher_rejections, 2)
        self.assertEqual(runtime.counters.backpressure_events, 1)

        ready[0] = True
        resumed = runtime.step()
        self.assertEqual(resumed.status, PLStepStatus.PUBLISHED)
        self.assertEqual(accepted_sequences, [0, 1])
        self.assertEqual(runtime.pending_updates, 0)
        self.assertEqual(runtime.counters.dropped_updates, 0)

    def test_external_rf_consumer_causes_a_loud_continuity_failure(self):
        radio = active_radio()
        rf = RFModel(radio)
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            lambda pair: True,
            realtime_pacing=False,
        )
        runtime.step()
        rf.synthesize(1)
        with self.assertRaisesRegex(PLRuntimeContinuityError, "expected 256, observed 257"):
            runtime.step()
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["counters"]["continuity_errors"], 1)
        self.assertIn("discontinuity", snapshot["last_error"])
        self.assertTrue(snapshot["failed"])
        with self.assertRaisesRegex(PLRuntimeError, "runtime is failed"):
            runtime.step()
        with self.assertRaisesRegex(PLRuntimeError, "cannot be restarted"):
            runtime.start()

    def test_mid_block_retune_is_rejected_instead_of_mislabelling_a_packet(self):
        radio = active_radio()
        rf = RFModel(radio)
        original_synthesize = rf.synthesize

        def synthesize_with_retune(count):
            first = original_synthesize(count // 2)
            radio.set_lo_frequency("rx", radio.rx_lo_hz + 1_000_000)
            return first + original_synthesize(count - len(first))

        rf.synthesize = synthesize_with_retune
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            lambda pair: self.fail("mixed-epoch data must not be published"),
            realtime_pacing=False,
        )

        with self.assertRaisesRegex(PLRuntimeContinuityError, "epoch changed inside"):
            runtime.step()
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["counters"]["continuity_errors"], 1)
        self.assertEqual(snapshot["counters"]["iq_frames_consumed"], 256)
        self.assertEqual(snapshot["counters"]["updates_generated"], 0)
        self.assertIn("epoch changed inside", snapshot["last_error"])

    def test_ensm_mute_is_an_epoch_boundary_and_cannot_mix_an_average(self):
        radio = active_radio()
        rf = RFModel(radio)
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=2_000),
            lambda pair: True,
            realtime_pacing=False,
        )
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        old_epoch = radio.config_epoch
        radio.set_ensm_state(ENSMState.ALERT)
        self.assertEqual(radio.config_epoch, old_epoch + 1)
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        emitted = runtime.step()
        self.assertEqual(emitted.status, PLStepStatus.PUBLISHED)
        self.assertEqual(emitted.packets[0].config_epoch, old_epoch + 1)
        self.assertEqual(emitted.packets[0].dropped_frames, 1)
        self.assertEqual(runtime.counters.reconfiguration_discarded_fft_frames, 1)

    def test_mid_block_ensm_transition_is_a_terminal_continuity_error(self):
        radio = active_radio()
        rf = RFModel(radio)
        original_synthesize = rf.synthesize

        def synthesize_with_mute(count):
            first = original_synthesize(count // 2)
            radio.set_ensm_state(ENSMState.ALERT)
            return first + original_synthesize(count - len(first))

        rf.synthesize = synthesize_with_mute
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            lambda pair: self.fail("mixed ENSM samples must not be published"),
            realtime_pacing=False,
        )
        with self.assertRaisesRegex(PLRuntimeContinuityError, "epoch changed inside"):
            runtime.step()
        snapshot = runtime.snapshot()
        self.assertTrue(snapshot["failed"])
        self.assertEqual(snapshot["counters"]["updates_generated"], 0)

    def test_worker_start_stop_pull_queue_and_lag_are_bounded_and_visible(self):
        radio = active_radio()
        rf = RFModel(radio)
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            publisher=None,
            pending_update_capacity=1,
            retry_interval_s=0.001,
        )
        runtime.start()
        self.assertTrue(runtime.wait_configured(1.0))
        deadline = time.monotonic() + 2.0
        while runtime.pending_updates == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(runtime.pending_updates, 1)
        self.assertTrue(runtime.stop(1.0))

        snapshot = runtime.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertFalse(snapshot["worker_busy"])
        self.assertGreaterEqual(snapshot["wall_elapsed_ns"], 0)
        self.assertGreaterEqual(snapshot["logical_elapsed_ns"], 0)
        self.assertEqual(
            snapshot["signed_lag_ns"], snapshot["lag_ns"] - snapshot["lead_ns"]
        )
        drained = runtime.drain()
        self.assertEqual(len(drained), 1)
        self.assertEqual(tuple(packet.channel for packet in drained[0]), (0, 1))
        self.assertEqual(runtime.counters.updates_drained, 1)
        self.assertEqual(runtime.counters.dropped_updates, 0)

    def test_drain_cannot_duplicate_an_inflight_publisher_pair(self):
        radio = active_radio()
        rf = RFModel(radio)
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            small_config(update_rate_hz=4_000),
            lambda pair: False,
            pending_update_capacity=2,
            realtime_pacing=False,
        )
        self.assertEqual(runtime.step().status, PLStepStatus.QUEUED)

        callback_entered = threading.Event()
        callback_release = threading.Event()
        accepted = []

        def blocking_publisher(pair):
            accepted.append(pair[0].sequence)
            callback_entered.set()
            callback_release.wait(1.0)
            return True

        runtime.set_publisher(blocking_publisher)
        step_errors = []
        step_thread = threading.Thread(
            target=lambda: self._capture_exception(runtime.step, step_errors)
        )
        step_thread.start()
        self.assertTrue(callback_entered.wait(1.0))

        drained = []
        drain_thread = threading.Thread(target=lambda: drained.extend(runtime.drain()))
        drain_thread.start()
        time.sleep(0.02)
        self.assertTrue(drain_thread.is_alive())

        callback_release.set()
        step_thread.join(1.0)
        drain_thread.join(1.0)
        self.assertFalse(step_thread.is_alive())
        self.assertFalse(drain_thread.is_alive())
        self.assertEqual(step_errors, [])
        self.assertEqual(drained, [])
        self.assertEqual(accepted, [0, 1])
        self.assertEqual(runtime.counters.updates_published, 2)
        self.assertEqual(runtime.counters.updates_drained, 0)

    def test_runtime_control_arguments_reject_boolean_and_nonfinite_values(self):
        radio = active_radio()
        rf = RFModel(radio)
        with self.assertRaisesRegex(TypeError, "realtime_pacing"):
            ContinuousPLSpectrumRuntime(rf, small_config(), realtime_pacing=1)
        with self.assertRaisesRegex(ValueError, "retry_interval_s"):
            ContinuousPLSpectrumRuntime(rf, small_config(), retry_interval_s=True)
        runtime = ContinuousPLSpectrumRuntime(rf, small_config())
        for value in (True, float("nan"), float("inf"), -1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runtime.wait_configured(value)
                with self.assertRaises(ValueError):
                    runtime.stop(value)

    @staticmethod
    def _capture_exception(operation, errors):
        try:
            operation()
        except Exception as error:  # pragma: no cover - assertion reports value
            errors.append(error)

    def test_full_65536_by_2_smoke_vector(self):
        radio = active_radio(sample_rate_hz=30_720_000, bandwidth_hz=18_000_000)
        rf = RFModel(radio)
        tone_bin = 17
        rf.add_baseband_tone(
            0,
            radio.sample_rate_hz * tone_bin / 65_536,
            amplitude=2_048,
        )
        published = []
        config = FFTConfig(
            fft_size=65_536,
            channels=2,
            window="rectangular",
            fftshift=False,
            averages=1,
            update_rate_hz=1_000,
            bin_start=0,
            bin_count=64,
            sample_rate_hz=61_440_000,
            center_frequency_hz=1,
            payload_encoding=PayloadEncoding.UINT16_LOG_POWER,
            full_scale=2_048,
        )
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            config,
            lambda pair: published.append(pair),
            realtime_pacing=False,
        )

        result = runtime.step()
        self.assertEqual(result.status, PLStepStatus.PUBLISHED)
        self.assertEqual(len(published), 1)
        pair = published[0]
        self.assertEqual(tuple(packet.fft_size for packet in pair), (65_536, 65_536))
        self.assertEqual(tuple(packet.bin_count for packet in pair), (64, 64))
        self.assertEqual(max(range(64), key=pair[0].values_dbfs.__getitem__), tone_bin)
        self.assertAlmostEqual(pair[0].values_dbfs[tone_bin], 0.0, delta=0.0001)
        self.assertTrue(all(value <= -199.0 for value in pair[1].values_dbfs))
        self.assertEqual(runtime.counters.iq_frames_consumed, 65_536)
        self.assertEqual(runtime.counters.fft_frames_processed, 1)
        self.assertEqual(runtime.counters.updates_generated, 1)
        self.assertEqual(runtime.counters.dropped_updates, 0)


if __name__ == "__main__":
    unittest.main()
