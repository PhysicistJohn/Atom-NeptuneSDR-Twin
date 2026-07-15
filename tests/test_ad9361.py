import unittest

from neptunesdr_twin.ad9361 import AD9361, Calibration, ENSMState, GainMode
from neptunesdr_twin.clock import VirtualClock
from neptunesdr_twin.errors import InvalidTransition, OutOfRange


class AD9361Tests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.radio = AD9361(self.clock)

    def test_reset_identity_and_legal_ensm_sequence(self):
        self.assertEqual(self.radio.read_register(0x037) & 0xF8, 0x08)
        self.assertEqual(
            self.radio.read_register(self.radio.REG_CH_1_OVERFLOW) & self.radio.BBPLL_LOCK,
            self.radio.BBPLL_LOCK,
        )
        self.assertEqual(
            self.radio.read_register(self.radio.REG_RX_CAL_STATUS) & self.radio.CP_CAL_VALID,
            self.radio.CP_CAL_VALID,
        )
        self.assertEqual(
            self.radio.read_register(self.radio.REG_TX_CAL_STATUS) & self.radio.CP_CAL_VALID,
            self.radio.CP_CAL_VALID,
        )
        self.assertEqual(
            self.radio.read_register(self.radio.REG_RX_CP_OVERRANGE_VCO_LOCK)
            & self.radio.VCO_LOCK,
            self.radio.VCO_LOCK,
        )
        self.assertEqual(
            self.radio.read_register(self.radio.REG_TX_CP_OVERRANGE_VCO_LOCK)
            & self.radio.VCO_LOCK,
            self.radio.VCO_LOCK,
        )
        self.assertEqual(self.radio.state, ENSMState.SLEEP)
        initial_epoch = self.radio.config_epoch
        with self.assertRaises(InvalidTransition):
            self.radio.set_ensm_state(ENSMState.FDD)
        self.assertEqual(self.radio.config_epoch, initial_epoch)
        self.radio.set_ensm_state(ENSMState.ALERT)
        self.assertEqual(self.radio.config_epoch, initial_epoch + 1)
        self.radio.set_ensm_state(ENSMState.ALERT)
        self.assertEqual(self.radio.config_epoch, initial_epoch + 1)
        self.radio.set_ensm_state(ENSMState.FDD)
        self.assertEqual(self.radio.config_epoch, initial_epoch + 2)
        self.assertEqual(self.radio.read_register(0x017) & 0x0F, 0x0A)

    def test_configuration_runs_deterministic_calibration(self):
        self.radio.initialize()
        self.assertFalse(self.radio.calibrated)
        self.assertGreater(int(self.radio.pending_calibrations), 0)
        self.clock.advance(self.radio.CALIBRATION_LATENCY_NS - 1)
        self.assertFalse(self.radio.calibrated)
        self.clock.advance(1)
        self.assertTrue(self.radio.calibrated)
        self.assertEqual(self.radio.read_register(self.radio.REG_CALIBRATION_CTRL), 0)

    def test_frequency_bandwidth_and_gain_boundaries(self):
        self.radio.set_lo_frequency("rx", 70_000_000)
        self.radio.set_lo_frequency("tx", 6_000_000_000)
        with self.assertRaises(OutOfRange):
            self.radio.set_lo_frequency("rx", 69_999_999)
        self.radio.set_rx_gain_mode(0, GainMode.MANUAL)
        self.radio.set_rx_gain(0, 12.0)
        self.assertEqual(self.radio.rx_channels[0].gain_db, 12.0)
        self.radio.set_tx_attenuation(1, 10.13)
        self.assertEqual(self.radio.tx_channels[1].attenuation_db, 10.25)

    def test_spi_burst_count_address_direction_and_soft_reset(self):
        # AD_WRITE | AD_CNT(2) | address 0x076.
        write_instruction = 0x8000 | (1 << 12) | 0x076
        tx = write_instruction.to_bytes(2, "big") + bytes([0x01, 0x20])
        self.assertEqual(self.radio.spi_transfer(tx), bytes(4))
        read_instruction = (1 << 12) | 0x076
        response = self.radio.spi_transfer(read_instruction.to_bytes(2, "big") + bytes(2))
        self.assertEqual(response[2:], bytes([0x01, 0x20]))
        self.radio.spi_transfer((0x8000).to_bytes(2, "big") + b"\x81")
        self.assertEqual(self.radio.state, ENSMState.SLEEP)

    def test_bandwidth_must_fit_sample_rate(self):
        self.radio.set_rf_bandwidth("rx", 1_000_000)
        self.radio.set_rf_bandwidth("tx", 1_000_000)
        self.radio.set_sample_rate(2_000_000)
        with self.assertRaises(OutOfRange):
            self.radio.set_rf_bandwidth("rx", 3_000_000)


if __name__ == "__main__":
    unittest.main()
