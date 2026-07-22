"""Focused regression tests for the RF, IIOD, and continuous-PL contacts.

These tests deliberately cross subsystem boundaries.  They protect the
properties host software relies on -- byte layout, ownership, ordering,
retune atomicity, and explicit loss -- without mirroring implementation
details one method at a time.
"""

import errno
import socket
import struct
import threading
import unittest

from neptunesdr_twin import NeptuneSDRTwin
from neptunesdr_twin.ad9361 import AD9361, ENSMState
from neptunesdr_twin.fft import FFTConfig, PacketFlag, PayloadEncoding, SpectrumPacket
from neptunesdr_twin.iio import IIODSession, MAX_IIOD_PAYLOAD_BYTES
from neptunesdr_twin.pl_runtime import (
    ContinuousPLSpectrumRuntime,
    PLRuntimeContinuityError,
    PLRuntimeError,
    PLStepStatus,
)
from neptunesdr_twin.rf import (
    BYTES_PER_FRAME,
    IQ,
    IQFrame,
    RFModel,
    pack_iq_frames,
    unpack_iq_frames,
)
from neptunesdr_twin.throughput import StreamRequest, Wideband50MHzProfile
from neptunesdr_twin.zynq import BootStage


def _active_radio():
    radio = AD9361()
    radio.set_rf_bandwidth("rx", 1_000_000)
    radio.set_rf_bandwidth("tx", 1_000_000)
    radio.set_sample_rate(1_024_000)
    radio.set_ensm_state(ENSMState.ALERT)
    radio.set_ensm_state(ENSMState.FDD)
    return radio


def _fft_config(update_rate_hz=2_000.0):
    # Live radio metadata replaces the placeholder rate, LO, and epoch.
    return FFTConfig(
        fft_size=256,
        channels=2,
        window="rectangular",
        fftshift=False,
        update_rate_hz=update_rate_hz,
        sample_rate_hz=61_440_000,
        center_frequency_hz=1,
        payload_encoding=PayloadEncoding.FLOAT32_DBFS,
        full_scale=1_000.0,
    )


def _configured_twin():
    twin = NeptuneSDRTwin()
    twin.boot_to_userspace()
    twin.radio.set_rf_bandwidth("rx", 1_000_000)
    twin.radio.set_rf_bandwidth("tx", 1_000_000)
    twin.radio.set_sample_rate(1_024_000)
    twin.radio.set_ensm_state(ENSMState.FDD)
    twin.configure_fft(_fft_config(update_rate_hz=4_000.0))
    return twin


class RFAndRateContactTests(unittest.TestCase):
    def test_iq_wire_abi_and_50mhz_rate_budget_are_unambiguous(self):
        frames = (
            IQFrame(IQ(-32768, 32767), IQ(-1, 1), sample_index=40),
            IQFrame(IQ(2, -2), IQ(1234, -1234), sample_index=41),
        )
        payload = pack_iq_frames(frames)
        self.assertEqual(len(payload), 2 * BYTES_PER_FRAME)
        self.assertEqual(payload[:8], struct.pack("<hhhh", -32768, 32767, -1, 1))
        decoded = unpack_iq_frames(payload, start_index=40, config_epoch=7)
        self.assertEqual(tuple(frame.channels for frame in decoded), tuple(frame.channels for frame in frames))
        self.assertEqual(tuple(frame.sample_index for frame in decoded), (40, 41))
        self.assertEqual({frame.config_epoch for frame in decoded}, {7})

        request = StreamRequest(61_440_000, channels=2, component_bits=16)
        self.assertEqual(request.bytes_per_complex_sample, 4)
        self.assertEqual(request.payload_bytes_per_second, 491_520_000)
        assessment = Wideband50MHzProfile().assess()
        self.assertEqual(assessment["analog_bandwidth_hz"], 50_000_000)
        self.assertTrue(assessment["internal"]["fits"])
        self.assertFalse(assessment["p210_host_claim"]["fits"])
        self.assertTrue(assessment["on_chip_fft_profile"]["spectrum_output"]["fits"])

    def test_rf_stream_is_consecutive_and_rejects_partial_frames(self):
        radio = _active_radio()
        rf = RFModel(radio)
        first = rf.synthesize(3)
        second = rf.synthesize(2)
        self.assertEqual(tuple(frame.sample_index for frame in first + second), tuple(range(5)))
        self.assertEqual({frame.config_epoch for frame in first + second}, {radio.config_epoch})
        with self.assertRaisesRegex(ValueError, "multiple of 8"):
            rf.stream_rx_bytes(7)


class ContinuousPLContactTests(unittest.TestCase):
    def test_full_50mhz_dual_65536_fft_crosses_the_nsft_wire_contract(self):
        radio = AD9361()
        radio.set_sample_rate(61_440_000)
        radio.set_rf_bandwidth("rx", 50_000_000)
        radio.set_rf_bandwidth("tx", 50_000_000)
        radio.set_ensm_state(ENSMState.ALERT)
        radio.set_ensm_state(ENSMState.FDD)
        rf = RFModel(radio, fifo_capacity_frames=65_536)
        tone_bin = 17
        rf.add_baseband_tone(
            0,
            radio.sample_rate_hz * tone_bin / 65_536,
            amplitude=2_048,
        )
        published = []
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            FFTConfig(
                fft_size=65_536,
                channels=2,
                window="rectangular",
                fftshift=False,
                update_rate_hz=1_000.0,
                bin_count=64,
                sample_rate_hz=61_440_000,
                payload_encoding=PayloadEncoding.UINT16_LOG_POWER,
                full_scale=2_048,
            ),
            lambda pair: published.append(pair),
            realtime_pacing=False,
        )
        result = runtime.step()
        self.assertEqual(result.status, PLStepStatus.PUBLISHED)
        self.assertEqual(runtime.counters.iq_frames_consumed, 65_536)
        self.assertEqual(tuple(packet.channel for packet in published[0]), (0, 1))
        for packet in published[0]:
            decoded = SpectrumPacket.unpack(packet.pack())
            self.assertEqual(decoded.fft_size, 65_536)
            self.assertEqual(decoded.sample_rate_hz, 61_440_000)
            self.assertEqual(decoded.bin_count, 64)
        self.assertEqual(
            max(range(64), key=published[0][0].values_dbfs.__getitem__),
            tone_bin,
        )
        self.assertEqual(runtime.counters.dropped_updates, 0)

    def test_atomic_pairs_follow_retunes_and_report_truncated_averages(self):
        radio = _active_radio()
        rf = RFModel(radio)
        rf.add_baseband_tone(0, radio.sample_rate_hz / 8, amplitude=1_000)
        published = []
        runtime = ContinuousPLSpectrumRuntime(
            rf,
            _fft_config(update_rate_hz=2_000.0),
            lambda pair: published.append(pair),
            realtime_pacing=False,
        )

        old_epoch = radio.config_epoch
        old_lo = radio.rx_lo_hz
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        radio.set_lo_frequency("rx", old_lo + 2_000_000)
        new_epoch = radio.config_epoch
        self.assertGreater(new_epoch, old_epoch)
        self.assertEqual(runtime.step().status, PLStepStatus.ACCUMULATING)
        result = runtime.step()

        self.assertEqual(result.status, PLStepStatus.PUBLISHED)
        self.assertEqual(len(published), 1)
        pair = published[0]
        self.assertEqual(tuple(packet.channel for packet in pair), (0, 1))
        for packet in pair:
            self.assertEqual(packet.sequence, pair[0].sequence)
            self.assertEqual(packet.config_epoch, new_epoch)
            self.assertEqual(packet.center_frequency_hz, old_lo + 2_000_000)
            self.assertEqual(packet.dropped_frames, 1)
            decoded = SpectrumPacket.unpack(packet.pack())
            self.assertTrue(decoded.flags & PacketFlag.DROPPED_FRAMES)
            self.assertTrue(decoded.flags & PacketFlag.INPUT_OVERRUN)
            self.assertEqual(decoded.config_epoch, new_epoch)
        self.assertEqual(runtime.counters.reconfiguration_discarded_fft_frames, 1)
        self.assertEqual(runtime.counters.dropped_updates, 0)

    def test_backpressure_stalls_input_and_discontinuity_becomes_terminal(self):
        radio = _active_radio()
        rf = RFModel(radio)
        accepting = [False]
        accepted_sequences = []

        def publish(pair):
            if not accepting[0]:
                return False
            accepted_sequences.append(pair[0].sequence)
            return True

        runtime = ContinuousPLSpectrumRuntime(
            rf,
            _fft_config(update_rate_hz=4_000.0),
            publish,
            pending_update_capacity=1,
            realtime_pacing=False,
        )
        self.assertEqual(runtime.step().status, PLStepStatus.QUEUED)
        consumed = runtime.counters.iq_frames_consumed
        sample_index = rf.next_sample_index
        self.assertEqual(runtime.step().status, PLStepStatus.BACKPRESSURED)
        self.assertEqual(runtime.counters.iq_frames_consumed, consumed)
        self.assertEqual(rf.next_sample_index, sample_index)
        self.assertEqual(runtime.counters.dropped_updates, 0)

        accepting[0] = True
        self.assertEqual(runtime.step().status, PLStepStatus.PUBLISHED)
        self.assertEqual(accepted_sequences, [0, 1])
        rf.synthesize(1)  # an illicit competing consumer creates a visible gap
        with self.assertRaisesRegex(PLRuntimeContinuityError, "discontinuity"):
            runtime.step()
        self.assertTrue(runtime.snapshot()["failed"])
        with self.assertRaises(PLRuntimeError):
            runtime.step()
        with self.assertRaises(PLRuntimeError):
            runtime.start()


class BoardAndIIODContactTests(unittest.TestCase):
    def test_pl_ownership_keeps_control_live_and_poweroff_is_bounded(self):
        twin = _configured_twin()
        session = IIODSession(twin.iio)
        callback_entered = threading.Event()
        callback_release = threading.Event()

        def blocked_sink(_pair):
            callback_entered.set()
            callback_release.wait(2.0)
            return True

        try:
            self.assertEqual(session.execute(b"OPEN iio:device3 2 0000000f\n"), b"0\n")
            runtime = twin.start_continuous_spectrum(
                publisher=blocked_sink,
                realtime_pacing=False,
            )
            self.assertTrue(runtime.wait_configured(1.0))
            self.assertTrue(callback_entered.wait(1.0))
            self.assertEqual(session.execute(b"READBUF iio:device3 16\n"), b"-%d\n" % errno.EPERM)

            new_lo = twin.radio.rx_lo_hz + 1_000_000
            value = str(new_lo).encode("ascii")
            command = b"WRITE iio:device0 OUTPUT altvoltage0 frequency %d\n" % len(value)
            self.assertEqual(session.execute(command, value), b"%d\n" % len(value))
            self.assertEqual(twin.radio.rx_lo_hz, new_lo)
            with self.assertRaisesRegex(RuntimeError, "manual FFT processing"):
                twin.process_fft_frame(([0j] * 256, [0j] * 256))

            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                twin.power_off(runtime_timeout_s=0.005)
            self.assertEqual(twin.zynq.boot_stage, BootStage.RUNNING)
            self.assertIs(twin.continuous_spectrum, runtime)
        finally:
            callback_release.set()
            twin.power_off(runtime_timeout_s=1.0)
        self.assertEqual(twin.zynq.boot_stage, BootStage.OFF)
        self.assertIsNone(twin.continuous_spectrum)

    def test_real_iiod_tcp_session_controls_and_streams_the_same_radio(self):
        twin = _configured_twin()
        address = twin.start_iiod(port=0)
        try:
            with socket.create_connection(address, timeout=1.0) as connection:
                connection.settimeout(1.0)
                stream = connection.makefile("rwb", buffering=0)
                stream.write(b"VERSION\n")
                self.assertEqual(stream.readline(), b"0.26.v0.26  \n")

                new_lo = twin.radio.rx_lo_hz + 3_000_000
                value = str(new_lo).encode("ascii")
                stream.write(
                    b"WRITE iio:device0 OUTPUT altvoltage0 frequency %d\n" % len(value)
                    + value
                )
                self.assertEqual(stream.readline(), b"%d\n" % len(value))
                self.assertEqual(twin.radio.rx_lo_hz, new_lo)

                stream.write(b"OPEN iio:device3 2 0000000f\n")
                self.assertEqual(stream.readline(), b"0\n")
                stream.write(b"READBUF iio:device3 16\n")
                self.assertEqual(stream.readline(), b"16\n")
                self.assertEqual(stream.readline(), b"0000000f\n")
                payload = stream.read(16)
                self.assertEqual(len(payload), 16)
                self.assertEqual(len(unpack_iq_frames(payload)), 2)
        finally:
            twin.close()

    def test_iiod_buffer_contract_fails_closed_on_shape_and_size(self):
        twin = NeptuneSDRTwin()
        session = IIODSession(twin.iio)
        self.assertEqual(session.execute(b"OPEN iio:device3 1 f\n"), b"-%d\n" % errno.EINVAL)
        self.assertEqual(session.execute(b"OPEN iio:device3 1 0000000f\n"), b"0\n")
        self.assertEqual(
            session.execute(b"READBUF iio:device3 %d\n" % (MAX_IIOD_PAYLOAD_BYTES + 1)),
            b"-%d\n" % errno.E2BIG,
        )
        with self.assertRaisesRegex(RuntimeError, "USB/IP export"):
            twin.start_usbip(port=0)
            try:
                twin.attach_usb(object())
            finally:
                twin.stop_usbip()
        twin.close()


if __name__ == "__main__":
    unittest.main()
