import struct
import unittest

from neptunesdr_twin.ad9361 import AD9361, ENSMState, GainMode
from neptunesdr_twin.errors import BufferOverrun, ContractViolation
from neptunesdr_twin.rf import (
    BYTES_PER_FRAME,
    BoundedFIFO,
    BufferUnderrun,
    IQ,
    IQFrame,
    OverrunPolicy,
    RFModel,
    UnderrunPolicy,
    pack_iq_frames,
    unpack_iq_frames,
    validate_frame_continuity,
)


def active_radio(sample_rate_hz=4_000_000, bandwidth_hz=4_000_000):
    radio = AD9361()
    # Narrow the reset bandwidth before lowering the sample rate.
    radio.set_rf_bandwidth("rx", bandwidth_hz)
    radio.set_rf_bandwidth("tx", bandwidth_hz)
    radio.set_sample_rate(sample_rate_hz)
    radio.set_ensm_state(ENSMState.ALERT)
    radio.set_ensm_state(ENSMState.FDD)
    return radio


class IQPackingTests(unittest.TestCase):
    def test_signed_little_endian_two_channel_order_is_exact(self):
        frame = IQFrame(IQ(0x1234, -2), IQ(-32768, 32767), 19, 7)
        expected = struct.pack("<hhhh", 0x1234, -2, -32768, 32767)
        self.assertEqual(frame.pack(), expected)
        self.assertEqual(len(expected), BYTES_PER_FRAME)

        decoded = unpack_iq_frames(expected, start_index=19, config_epoch=7)
        self.assertEqual(decoded, (frame,))

    def test_multi_frame_packing_is_time_major_and_metadata_free(self):
        frames = (
            IQFrame(IQ(1, 2), IQ(3, 4), 100, 11),
            IQFrame(IQ(5, 6), IQ(7, 8), 101, 12),
        )
        self.assertEqual(
            pack_iq_frames(frames),
            struct.pack("<hhhhhhhh", 1, 2, 3, 4, 5, 6, 7, 8),
        )
        decoded = unpack_iq_frames(pack_iq_frames(frames), start_index=100, config_epoch=99)
        self.assertEqual([item.sample_index for item in decoded], [100, 101])
        self.assertEqual([item.config_epoch for item in decoded], [99, 99])

    def test_misaligned_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            unpack_iq_frames(b"\x00" * (BYTES_PER_FRAME - 1))


class RFSignalTests(unittest.TestCase):
    def test_quarter_rate_tone_has_deterministic_frequency_and_phase(self):
        radio = active_radio()
        model = RFModel(radio)
        model.add_rx_tone(0, radio.rx_lo_hz + 1_000_000, amplitude=1000)

        samples = model.synthesize(8)
        observed = [(frame.channel0.i, frame.channel0.q) for frame in samples]
        self.assertEqual(
            observed,
            [
                (1000, 0),
                (0, 1000),
                (-1000, 0),
                (0, -1000),
                (1000, 0),
                (0, 1000),
                (-1000, 0),
                (0, -1000),
            ],
        )

    def test_two_channels_remain_independent_and_pack_in_lane_order(self):
        radio = active_radio()
        model = RFModel(radio)
        model.add_baseband_tone(0, 0, amplitude=101, phase_rad=0)
        model.add_baseband_tone(1, 0, amplitude=202, phase_rad=3.141592653589793 / 2)

        frame = model.synthesize(1)[0]
        self.assertEqual(frame, IQFrame(IQ(101, 0), IQ(0, 202), 0, radio.config_epoch, 0))
        self.assertEqual(frame.pack(), struct.pack("<hhhh", 101, 0, 0, 202))

    def test_rx_lo_and_bandwidth_form_an_ideal_complex_passband(self):
        radio = active_radio(sample_rate_hz=4_000_000, bandwidth_hz=1_000_000)
        model = RFModel(radio)
        model.add_baseband_tone(0, 400_000, amplitude=1000)
        model.add_baseband_tone(1, 600_000, amplitude=1000)
        frames = model.synthesize(5)
        self.assertTrue(any(frame.channel0 != IQ.zero() for frame in frames))
        self.assertTrue(all(frame.channel1 == IQ.zero() for frame in frames))

    def test_manual_gain_saturates_both_polarities_and_counts_clipping(self):
        radio = active_radio()
        radio.set_rx_gain_mode(0, GainMode.MANUAL)
        radio.set_rx_gain(0, 20.0)
        model = RFModel(radio)
        model.add_baseband_tone(0, 1_000_000, amplitude=4000)

        samples = model.synthesize(3)
        self.assertEqual(samples[0].channel0.i, 32767)
        self.assertEqual(samples[1].channel0.q, 32767)
        self.assertEqual(samples[2].channel0.i, -32768)
        self.assertEqual(model.clipped_frames, 3)
        self.assertEqual(model.clipped_components, [3, 0])

    def test_seeded_noise_is_reproducible_and_nonzero(self):
        radio_a = active_radio()
        radio_b = active_radio()
        first = RFModel(radio_a, noise_seed=0xBAD5EED)
        second = RFModel(radio_b, noise_seed=0xBAD5EED)
        first.set_noise_rms(0, 30)
        second.set_noise_rms(0, 30)
        a = first.synthesize(12)
        b = second.synthesize(12)
        self.assertEqual([item.channel0 for item in a], [item.channel0 for item in b])
        self.assertTrue(any(item.channel0 != IQ.zero() for item in a))

    def test_tx_rx_loopback_applies_lo_offset_coupling_and_attenuation(self):
        radio = active_radio()
        radio.set_lo_frequency("tx", radio.rx_lo_hz + 1_000_000)
        radio.set_tx_attenuation(0, 0.0)
        model = RFModel(radio)
        model.configure_loopback(coupling_db=0.0)
        tx = [IQFrame(IQ(1000, 0), IQ.zero(), index, radio.config_epoch) for index in range(4)]
        model.write_tx_frames(tx)

        samples = model.synthesize(4)
        self.assertEqual(
            [(frame.channel0.i, frame.channel0.q) for frame in samples],
            [(1000, 0), (0, 1000), (-1000, 0), (0, -1000)],
        )
        self.assertEqual(model.tx_fifo.stats.underrun_frames, 0)

    def test_loopback_matrix_keeps_two_by_two_routing_explicit(self):
        radio = active_radio()
        radio.set_lo_frequency("tx", radio.rx_lo_hz)
        radio.set_tx_attenuation(0, 0.0)
        radio.set_tx_attenuation(1, 0.0)
        model = RFModel(radio)
        model.configure_loopback(coupling_db=-120.0)
        model.set_coupling(0, 0, None)
        model.set_coupling(1, 0, 0.0)
        model.write_tx_frames([IQFrame(IQ(333, -444), IQ.zero())])
        frame = model.synthesize(1)[0]
        self.assertEqual(frame.channel0, IQ.zero())
        self.assertEqual(frame.channel1, IQ(333, -444))

    def test_samples_are_continuous_and_config_epoch_boundary_is_visible(self):
        radio = active_radio()
        model = RFModel(radio)
        first = model.synthesize(3)
        old_epoch = radio.config_epoch
        radio.set_lo_frequency("rx", radio.rx_lo_hz + 100_000)
        second = model.synthesize(2)

        self.assertEqual(validate_frame_continuity(first + second), 5)
        self.assertEqual([frame.sample_index for frame in first + second], list(range(5)))
        self.assertEqual({frame.config_epoch for frame in first}, {old_epoch})
        self.assertEqual({frame.config_epoch for frame in second}, {radio.config_epoch})
        self.assertEqual(
            model.configuration_boundaries,
            ((0, old_epoch), (3, radio.config_epoch)),
        )
        with self.assertRaises(ContractViolation):
            validate_frame_continuity((first[0], second[0]))


class BoundedFIFOTests(unittest.TestCase):
    def test_raise_policies_are_atomic_and_counters_prevent_silent_loss(self):
        fifo = BoundedFIFO[int](2)
        fifo.push([10, 11])
        with self.assertRaises(BufferOverrun):
            fifo.push([12])
        self.assertEqual(fifo.pop(2), (10, 11))
        with self.assertRaises(BufferUnderrun):
            fifo.pop(1)
        self.assertEqual(fifo.stats.overrun_events, 1)
        self.assertEqual(fifo.stats.overrun_frames, 1)
        self.assertEqual(fifo.stats.underrun_events, 1)
        self.assertEqual(fifo.stats.underrun_frames, 1)

    def test_drop_newest_and_drop_oldest_report_every_lost_frame(self):
        newest = BoundedFIFO[int](2, overrun_policy=OverrunPolicy.DROP_NEWEST)
        self.assertEqual(newest.push([1, 2, 3, 4]), 2)
        self.assertEqual(newest.pop(2), (1, 2))
        self.assertEqual(newest.stats.overrun_frames, 2)

        oldest = BoundedFIFO[int](3, overrun_policy=OverrunPolicy.DROP_OLDEST)
        oldest.push([1, 2])
        self.assertEqual(oldest.push([3, 4, 5]), 3)
        self.assertEqual(oldest.pop(3), (3, 4, 5))
        self.assertEqual(oldest.stats.overrun_frames, 2)

    def test_zero_fill_is_explicit_and_underrun_counted(self):
        fifo = BoundedFIFO[int](2, underrun_policy=UnderrunPolicy.ZERO_FILL, fill_factory=lambda: 0)
        fifo.push([9])
        self.assertEqual(fifo.pop(3), (9, 0, 0))
        self.assertEqual(fifo.stats.popped_frames, 1)
        self.assertEqual(fifo.stats.underrun_frames, 2)

    def test_rx_fifo_overrun_advances_sequence_and_exposes_gap(self):
        radio = active_radio()
        model = RFModel(
            radio,
            fifo_capacity_frames=2,
            rx_overrun_policy=OverrunPolicy.DROP_OLDEST,
        )
        model.produce(2)
        model.produce(2)
        retained = model.read_rx_frames(2)
        self.assertEqual([item.sample_index for item in retained], [2, 3])
        self.assertEqual(model.rx_fifo.stats.overrun_frames, 2)
        self.assertEqual(model.next_sample_index, 4)


if __name__ == "__main__":
    unittest.main()
