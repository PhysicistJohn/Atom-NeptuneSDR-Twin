import unittest

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.throughput import (
    GIGABIT_ETHERNET_THEORETICAL,
    P210_HOST_12MSPS_ONE_CHANNEL,
    USB2_THEORETICAL,
    StreamRequest,
    Wideband50MHzProfile,
    maximum_capture_seconds,
    required_decimation,
)


class WidebandContractTests(unittest.TestCase):
    def test_ad9361_accepts_50mhz_profile_at_wide_sample_rate(self):
        radio = AD9361()
        radio.set_sample_rate(61_440_000)
        radio.set_rf_bandwidth("rx", 50_000_000)
        radio.set_rf_bandwidth("tx", 50_000_000)
        self.assertEqual((radio.rx_bandwidth_hz, radio.tx_bandwidth_hz), (50_000_000,) * 2)

    def test_raw_2x2_rate_is_not_a_host_transport_claim(self):
        request = StreamRequest(61_440_000, channels=2, component_bits=16)
        self.assertEqual(request.payload_bytes_per_second, 491_520_000)
        self.assertFalse(P210_HOST_12MSPS_ONE_CHANNEL.evaluate(request).fits)
        self.assertFalse(USB2_THEORETICAL.evaluate(request).fits)
        self.assertFalse(GIGABIT_ETHERNET_THEORETICAL.evaluate(request).fits)
        self.assertEqual(required_decimation(request, P210_HOST_12MSPS_ONE_CHANNEL), 11)

    def test_one_channel_still_exceeds_gigabit_at_61_44_msps(self):
        request = StreamRequest(61_440_000, channels=1)
        self.assertEqual(request.payload_bytes_per_second, 245_760_000)
        self.assertFalse(GIGABIT_ETHERNET_THEORETICAL.evaluate(request).fits)

    def test_burst_duration_is_explicit(self):
        request = StreamRequest(61_440_000, channels=2)
        duration = maximum_capture_seconds(512 * 1024 * 1024, request)
        self.assertAlmostEqual(duration, 1.0922666667, places=6)
        report = Wideband50MHzProfile().assess()
        self.assertEqual(report["analog_bandwidth_hz"], 50_000_000)
        self.assertFalse(report["p210_host_claim"]["fits"])


if __name__ == "__main__":
    unittest.main()
