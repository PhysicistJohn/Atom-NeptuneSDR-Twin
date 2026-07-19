"""Every documented CLI command emits parseable JSON with its contracted shape."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from neptunesdr_twin.cli import main


def invoke(*arguments):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = main(arguments)
    return status, json.loads(output.getvalue())


class CLISurfaceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
