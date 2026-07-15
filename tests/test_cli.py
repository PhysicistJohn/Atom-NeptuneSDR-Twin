import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from neptunesdr_twin.cli import _distribution_contract_path, _wait_for_stop, main


def invoke(*arguments):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = main(arguments)
    return status, json.loads(output.getvalue())


class CLITests(unittest.TestCase):
    def test_distribution_contract_path_uses_distribution_relative_data(self):
        with tempfile.TemporaryDirectory() as directory:
            contract = Path(directory) / "contracts.json"
            contract.write_text("{}", encoding="utf-8")

            class Distribution:
                files = [Path("../../../share/neptunesdr-twin/contracts.json")]

                @staticmethod
                def locate_file(_item):
                    return contract

            with mock.patch(
                "neptunesdr_twin.cli.metadata.distribution",
                return_value=Distribution(),
            ):
                self.assertEqual(_distribution_contract_path(), contract)

    def test_distribution_contract_path_supports_pip_target_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "share" / "neptunesdr-twin" / "contracts.json"
            contract.parent.mkdir(parents=True)
            contract.write_text("{}", encoding="utf-8")

            class Distribution:
                files = []

                @staticmethod
                def locate_file(_item):
                    return root

            with mock.patch(
                "neptunesdr_twin.cli.metadata.distribution",
                return_value=Distribution(),
            ):
                self.assertEqual(_distribution_contract_path(), contract)

    def test_info_and_wideband_are_machine_readable(self):
        status, info = invoke("info")
        self.assertEqual(status, 0)
        self.assertEqual(info["resolved"]["soc"], "XC7Z020-CLG400I")
        status, wideband = invoke("wideband")
        self.assertEqual(status, 0)
        self.assertEqual(wideband["analog_bandwidth_hz"], 50_000_000)
        self.assertFalse(wideband["p210_host_claim"]["fits"])
        self.assertTrue(wideband["on_chip_fft_profile"]["spectrum_output"]["fits"])

    def test_fft_plan_is_machine_readable_and_transport_safe(self):
        status, payload = invoke("fft-plan")
        self.assertEqual(status, 0)
        self.assertEqual(payload["configuration"]["fft_size"], 65_536)
        self.assertTrue(payload["pl_ingress"]["fits"])
        self.assertTrue(payload["host_egress"]["fits"])
        self.assertEqual(
            payload["packet_contract"],
            "NSFT version 1, network byte order, CRC32",
        )

    def test_usb_and_contracts_are_consistent(self):
        status, usb = invoke("usb")
        self.assertEqual(status, 0)
        self.assertEqual(usb["interfaces"], 6)
        status, contracts = invoke("contracts")
        self.assertEqual(status, 0)
        self.assertTrue(contracts["compatible"])

    def test_snapshot_file_and_server_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            status, result = invoke("snapshot", "--output", str(path))
            self.assertEqual(status, 0)
            self.assertTrue(path.exists())
            self.assertEqual(len(result["sha256"]), 64)
        status, result = invoke("serve", "--port", "0", "--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("would_listen", result)
        self.assertEqual(result["libiio_uri"], "ip:127.0.0.1:0")
        status, result = invoke(
            "usbip-serve",
            "--port",
            "3240",
            "--iiod-backend",
            "127.0.0.1:30431",
            "--dry-run",
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["busid"], "1-1")
        self.assertEqual(result["native_iio"], "tcp:127.0.0.1:30431")

    def test_complete_appliance_dry_run_resolves_wideband_contract(self):
        status, result = invoke("appliance", "--dry-run")
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "validated-dry-run")
        self.assertEqual(result["radio"]["rx_bandwidth_hz"], 50_000_000)
        self.assertEqual(result["spectrum"]["fft_size"], 65_536)
        self.assertEqual(result["spectrum"]["bin_count"], 65_536)
        self.assertEqual(result["fft_frames_per_update"], 47)
        self.assertFalse(result["continuous_dataflow"]["silent_drops"])
        self.assertIn("exclusive RF owner", result["continuous_dataflow"]["raw_iq_reads"])

    def test_complete_appliance_can_bind_every_local_contact_and_stop(self):
        status, result = invoke(
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
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["fft_frames_per_update"], 1)
        self.assertEqual(result["endpoints"]["usbip_busid"], "1-1")
        for name in ("iiod", "spectrum", "usbip"):
            self.assertNotIn(":0", result["endpoints"][name])

    def test_listener_ports_and_durations_fail_closed_at_argument_parsing(self):
        for arguments in (
            ("serve", "--port", "-1", "--dry-run"),
            ("usbip-serve", "--port", "65536", "--dry-run"),
            ("serve", "--duration", "nan", "--dry-run"),
            ("usbip-serve", "--duration", "inf", "--dry-run"),
            ("appliance", "--duration", "-0.1", "--dry-run"),
        ):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        main(arguments)

    def test_default_tones_cannot_silently_alias_at_a_custom_sample_rate(self):
        with self.assertRaisesRegex(ValueError, "strictly inside Nyquist"):
            main(
                (
                    "appliance",
                    "--sample-rate",
                    "1024000",
                    "--bandwidth",
                    "1000000",
                    "--dry-run",
                )
            )

    def test_bounded_wait_checks_health_even_for_zero_duration(self):
        calls = []

        def unhealthy():
            calls.append(True)
            raise RuntimeError("worker failed")

        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            _wait_for_stop(threading.Event(), 0.0, unhealthy)
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
