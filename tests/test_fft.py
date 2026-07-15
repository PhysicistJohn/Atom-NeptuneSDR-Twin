import cmath
import math
import unittest

from neptunesdr_twin.fft import (
    BackpressureMode,
    FFTConfig,
    FFTWindow,
    PACKET_OVERHEAD_BYTES,
    PLThroughputContract,
    PacketCRCError,
    PacketFlag,
    PayloadEncoding,
    PowerAccumulator,
    ProcessingStatus,
    SpectrumPacket,
    SpectrumProcessor,
    calculate_output_rate_budget,
    coherent_gain,
    power_spectrum_dbfs,
    radix2_fft,
    unpack_spectrum_packet,
    window_coefficients,
    windowed_fft,
)


class ReferenceFFTTests(unittest.TestCase):
    def assertComplexAlmostEqual(self, actual, expected, places=11):
        self.assertAlmostEqual(actual.real, expected.real, places=places)
        self.assertAlmostEqual(actual.imag, expected.imag, places=places)

    def test_tiny_radix2_impulse_and_known_dft(self):
        self.assertEqual(radix2_fft([1, 0, 0, 0]), (1 + 0j,) * 4)
        result = radix2_fft([1, 2, 3, 4])
        expected = (10 + 0j, -2 + 2j, -2 + 0j, -2 - 2j)
        for actual, wanted in zip(result, expected):
            self.assertComplexAlmostEqual(actual, wanted)

    def test_periodic_window_coherent_gain_and_tone_normalization(self):
        size = 256
        tone_bin = 19
        tone = [cmath.exp(2j * math.pi * tone_bin * n / size) for n in range(size)]
        expected_gains = {
            FFTWindow.RECTANGULAR: 1.0,
            FFTWindow.HANN: 0.5,
            FFTWindow.BLACKMAN: 0.42,
        }
        for window, expected_gain in expected_gains.items():
            with self.subTest(window=window):
                self.assertAlmostEqual(
                    coherent_gain(window_coefficients(size, window)), expected_gain, places=12
                )
                bins = windowed_fft(tone, window, coherent_gain_normalization=True)
                self.assertAlmostEqual(abs(bins[tone_bin]), 1.0, places=11)

    def test_fftshift_and_selected_bin_range(self):
        size = 256
        negative_bin = -7
        tone = [cmath.exp(2j * math.pi * negative_bin * n / size) for n in range(size)]
        config = FFTConfig(
            fft_size=size,
            channels=1,
            window="rectangular",
            fftshift=True,
            sample_rate_hz=size,
            update_rate_hz=1,
            bin_start=size // 2 - 10,
            bin_count=20,
        )
        power = power_spectrum_dbfs(tone, config)
        self.assertEqual(len(power), 20)
        # Shifted bin -7 is full-bin index 128-7, or selected index 3.
        self.assertEqual(max(range(len(power)), key=power.__getitem__), 3)
        self.assertAlmostEqual(power[3], 0.0, places=10)

    def test_config_accepts_full_65536_without_executing_large_fft(self):
        config = FFTConfig(fft_size=65_536, channels=2)
        self.assertEqual(config.bin_count, 65_536)
        with self.assertRaises(ValueError):
            FFTConfig(fft_size=1000)
        with self.assertRaises(ValueError):
            FFTConfig(fft_size=131_072)


class AccumulationAndPLContractTests(unittest.TestCase):
    def test_linear_power_accumulation(self):
        accumulator = PowerAccumulator(bin_count=2, frames=2)
        self.assertIsNone(accumulator.add((1.0, 9.0)))
        self.assertEqual(accumulator.add((3.0, 1.0)), (2.0, 5.0))
        self.assertEqual(accumulator.count, 0)

    def test_update_rate_controls_average_depth(self):
        config = FFTConfig(
            fft_size=256,
            channels=1,
            sample_rate_hz=2560,
            averages=2,
            update_rate_hz=2,
        )
        # Ten FFT frames/s, capped at two updates/s, so each result averages 5.
        self.assertEqual(config.frames_per_update, 5)
        self.assertEqual(config.effective_update_rate_hz, 2.0)

    def test_pl_contract_is_aggregate_across_channels(self):
        fits = PLThroughputContract(
            stream_clock_hz=100_000_000,
            lanes=2,
            input_sample_rate_hz=61_440_000,
            channels=2,
        )
        assessment = fits.assess(FFTConfig(fft_size=65_536, channels=2))
        self.assertTrue(assessment.fits)
        self.assertEqual(assessment.required_complex_samples_per_second, 122_880_000)
        self.assertEqual(assessment.capacity_complex_samples_per_second, 200_000_000)

        too_narrow = PLThroughputContract(
            stream_clock_hz=100_000_000,
            lanes=1,
            input_sample_rate_hz=61_440_000,
            channels=2,
        )
        self.assertFalse(too_narrow.assess().fits)

    def test_processor_reports_overrun_and_carries_it_in_next_packet(self):
        config = FFTConfig(
            fft_size=256,
            channels=1,
            window="rectangular",
            fftshift=False,
            averages=1,
            update_rate_hz=1,
            sample_rate_hz=256,
            payload_encoding=PayloadEncoding.FLOAT32_DBFS,
        )
        processor = SpectrumProcessor(config)
        overrun = processor.record_overrun("test source overflow")
        self.assertEqual(overrun.status, ProcessingStatus.OVERRUN)
        self.assertEqual(overrun.dropped_frames, 1)
        result = processor.process_frame([1 + 0j] * 256, timestamp_ns=123)
        self.assertEqual(result.status, ProcessingStatus.EMITTED)
        self.assertEqual(result.packets[0].dropped_frames, 1)
        self.assertEqual(result.packets[0].overrun_events, 1)
        self.assertTrue(result.packets[0].pack())

    def test_result_backpressure_queues_or_drops_explicitly(self):
        config = FFTConfig(
            fft_size=256,
            channels=1,
            window="rectangular",
            fftshift=False,
            sample_rate_hz=256,
            update_rate_hz=1,
        )
        buffering = PLThroughputContract(
            stream_clock_hz=10_000_000,
            lanes=1,
            input_sample_rate_hz=256,
            channels=1,
            output_backpressure=BackpressureMode.READY_VALID,
            result_fifo_updates=1,
        )
        processor = SpectrumProcessor(config, buffering)
        blocked = processor.process_frame([1 + 0j] * 256, sink_ready=False)
        self.assertEqual(blocked.status, ProcessingStatus.BACKPRESSURED)
        self.assertEqual(processor.pending_updates, 1)
        delivered = processor.drain(sink_ready=True)
        self.assertEqual(delivered.status, ProcessingStatus.EMITTED)
        self.assertEqual(len(delivered.packets), 1)

        no_buffer = PLThroughputContract(
            stream_clock_hz=10_000_000,
            lanes=1,
            input_sample_rate_hz=256,
            channels=1,
            output_backpressure=BackpressureMode.NONE,
            result_fifo_updates=0,
        )
        dropped = SpectrumProcessor(config, no_buffer).process_frame(
            [1 + 0j] * 256, sink_ready=False
        )
        self.assertEqual(dropped.status, ProcessingStatus.DROPPED)
        self.assertEqual(dropped.dropped_updates, 1)


class PacketAndBudgetTests(unittest.TestCase):
    def make_packet(self, encoding):
        return SpectrumPacket(
            sequence=0x1_0000_0002,
            channel=1,
            fft_size=65_536,
            sample_rate_hz=61_440_000,
            center_frequency_hz=5_800_000_000,
            timestamp_ns=1_725_000_000_123_456_789,
            config_epoch=7,
            bin_start=101,
            values_dbfs=(-200.0, -93.125, -0.01, 0.0),
            encoding=encoding,
            dropped_frames=3,
            overrun_events=1,
            dropped_updates=2,
        )

    def test_float32_packet_round_trip_and_crc(self):
        packet = self.make_packet(PayloadEncoding.FLOAT32_DBFS)
        wire = packet.pack()
        self.assertEqual(len(wire), PACKET_OVERHEAD_BYTES + 4 * 4)
        decoded = unpack_spectrum_packet(wire)
        self.assertEqual(decoded.sequence, packet.sequence)
        self.assertEqual(decoded.channel, packet.channel)
        self.assertEqual(decoded.fft_size, packet.fft_size)
        self.assertEqual(decoded.sample_rate_hz, packet.sample_rate_hz)
        self.assertEqual(decoded.center_frequency_hz, packet.center_frequency_hz)
        self.assertEqual(decoded.timestamp_ns, packet.timestamp_ns)
        self.assertEqual(decoded.config_epoch, packet.config_epoch)
        self.assertEqual(decoded.bin_start, packet.bin_start)
        self.assertEqual(decoded.bin_count, 4)
        self.assertTrue(decoded.flags & PacketFlag.INPUT_OVERRUN)
        for actual, expected in zip(decoded.values_dbfs, packet.values_dbfs):
            self.assertAlmostEqual(actual, expected, places=4)

        corrupt = bytearray(wire)
        corrupt[-5] ^= 0x01
        with self.assertRaises(PacketCRCError):
            unpack_spectrum_packet(corrupt)

    def test_uint16_log_packet_round_trip_is_quantized(self):
        packet = self.make_packet(PayloadEncoding.UINT16_LOG_POWER)
        decoded = SpectrumPacket.unpack(packet.pack())
        self.assertEqual(decoded.encoding, PayloadEncoding.UINT16_LOG_POWER)
        self.assertEqual(decoded.bin_count, 4)
        for actual, expected in zip(decoded.values_dbfs, packet.values_dbfs):
            self.assertAlmostEqual(actual, expected, delta=0.0051)

    def test_two_full_65536_uint16_spectra_at_20hz_fit_vendor_budget(self):
        budget = calculate_output_rate_budget(
            fft_size=65_536,
            channels=2,
            updates_per_second=20,
            encoding=PayloadEncoding.UINT16_LOG_POWER,
        )
        self.assertEqual(budget.payload_bytes_per_update, 262_144)
        self.assertEqual(budget.payload_bytes_per_second, 5_242_880)
        self.assertEqual(budget.payload_megabytes_per_second, 5.24288)
        self.assertGreater(budget.wire_bytes_per_second, budget.payload_bytes_per_second)
        self.assertTrue(budget.fits)
        self.assertTrue(budget.fits_vendor_claim)

    def test_bin_selection_reduces_output_budget(self):
        full = calculate_output_rate_budget(65_536, channels=2, updates_per_second=20)
        selected = calculate_output_rate_budget(
            65_536, channels=2, updates_per_second=20, bin_start=1000, bin_count=4096
        )
        self.assertLess(selected.wire_bytes_per_second, full.wire_bytes_per_second)


if __name__ == "__main__":
    unittest.main()
