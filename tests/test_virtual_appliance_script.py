"""Lifecycle regressions for the composed firmware/USB appliance wrapper."""

from pathlib import Path
import os
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
APPLIANCE = ROOT / "scripts" / "run_virtual_appliance.sh"


class VirtualApplianceScriptTests(unittest.TestCase):
    def test_cleanup_has_bounded_term_kill_and_zombie_handling(self):
        source = APPLIANCE.read_text()
        for token in (
            "SHUTDOWN_GRACE_SECONDS=5",
            "KILL_GRACE_SECONDS=1",
            "ps -p",
            "''|Z*) return 1",
            "kill -TERM",
            "kill -KILL",
            "not waiting forever",
            "reap_process_bounded",
        ):
            self.assertIn(token, source)

    def test_dry_run_rejects_invalid_environment_ports_before_starting_children(self):
        environment = os.environ.copy()
        environment["P210_IIO_HOST_PORT"] = "65536"
        result = subprocess.run(
            [str(APPLIANCE), "--dry-run"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid port", result.stderr)
        self.assertNotIn("firmware=", result.stdout)


if __name__ == "__main__":
    unittest.main()
