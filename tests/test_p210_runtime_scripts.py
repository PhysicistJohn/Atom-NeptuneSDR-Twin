"""Regression checks for the native P210 QEMU build and runtime wrappers."""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "build_p210_qemu.sh"
RUN = ROOT / "scripts" / "run_p210_firmware.sh"
APPLIANCE = ROOT / "scripts" / "run_virtual_appliance.sh"
ACCEPT = ROOT / "scripts" / "accept_virtual_twin.sh"
PATCH = ROOT / "qemu" / "patches" / "0001-p210-zynq-devices.patch"
PREPARE = ROOT / "scripts" / "prepare_runtime.py"


class P210RuntimeScriptTests(unittest.TestCase):
    def test_wrappers_are_executable_and_shell_syntax_is_valid(self) -> None:
        for script in (BUILD, RUN, APPLIANCE, ACCEPT):
            self.assertTrue(script.stat().st_mode & stat.S_IXUSR, script)
            subprocess.run(["sh", "-n", str(script)], check=True)

    def test_build_is_source_and_toolchain_locked(self) -> None:
        source = BUILD.read_text()
        for token in (
            "QEMU_VERSION=10.0.2",
            "ef786f2398cb5184600f69aef4d5d691efd44576a3cff4126d38d4c6fec87759",
            "MICROMAMBA_VERSION=2.8.1-0",
            "de71a646b73af92dd663e6ddc78993a6a4d47ea28b5d8908c3cc2b9c3077e528",
            "libslirp=4.4.0",
            "--target-list=arm-softmmu",
            "--enable-slirp",
            ".p210-integration.sha256",
            'PATCH_SHA256=$(sha256_file "$PATCH")',
            "-machine xilinx-zynq-a9,help",
        ):
            self.assertIn(token, source)

    def test_patch_integrates_all_p210_contacts(self) -> None:
        patch = PATCH.read_text()
        for token in (
            '#include "hw/misc/p210_sdr.h"',
            '#include "hw/misc/p210_fft.h"',
            "TYPE_P210_AD9361",
            "TYPE_P210_SDR",
            "TYPE_P210_FFT",
            "0x79020000",
            "0x79024000",
            "0x7c400000",
            "0x7c420000",
            "0x7c450000",
            "pic[57]",
            "pic[56]",
            "pic[58]",
            "p210_start_secondary_async",
            'object_class_property_add_bool(oc, "p210"',
            "files('p210_ad9361.c')",
        ):
            self.assertIn(token, patch)

    def test_default_run_is_bounded_full_fft_hardware_workflow(self) -> None:
        source = RUN.read_text()
        for token in (
            "zig=0.14.1",
            '"$ROOT/scripts/build_guest_fft.sh"',
            "p210-sd-boot plutosdr-fw-v0.39",
            '--fft-streamer "$GUEST"',
            "xilinx-zynq-a9,p210=on",
            "-smp 2",
            "mem=384M",
            "hostfwd=tcp:127.0.0.1:${IIO_PORT}-10.0.2.15:30431",
            "hostfwd=tcp:127.0.0.1:${FFT_PORT}-10.0.2.15:30432",
            'socket,id=uart1,host=127.0.0.1,port=${UART_PORT}',
            "-serial chardev:uart1",
            'set -- "$@" -gdb "tcp:127.0.0.1:$GDB_PORT"',
            'trap cleanup 0',
            'MODE=selftest',
            '"$ROOT/scripts/build_host_libiio.sh"',
            '"$ROOT/scripts/build_host_libiio.sh" --verify',
            '"$ROOT/scripts/host_iio.sh" --uri "$iio_uri" info -T 1000',
            'IIO_REPORT=${P210_IIO_REPORT:-"$RUNTIME/p210-qemu-iio-info.txt"}',
            "IIO context has 5 devices:",
            "cf-ad9361-lpc (buffer capable)",
            "rf_bandwidth value: 50000000",
            "sampling_frequency value: 61440000",
            'python3 "$ROOT/scripts/capture_guest_fft.py"',
            "AD936x Rev 2 successfully initialized",
            "NEPTUNE_RUNTIME cpu-online=0-1",
            "rf-bandwidth=50000000 sample-rate=61440000",
            "input=iio-dmac-cpu-copy",
            "bins=131072 bytes=262288",
            "SHUTDOWN_GRACE_SECONDS=5",
            'kill -KILL "$qemu_pid"',
            "explicit allowlist",
            "P210_RUNTIME PASS",
        ):
            self.assertIn(token, source)

    def test_help_exposes_selftest_and_long_running_modes(self) -> None:
        result = subprocess.run(
            [str(RUN), "--help"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertIn("bounded self-test", result.stdout)
        self.assertIn("--serve", result.stdout)
        self.assertIn("--no-build", result.stdout)
        self.assertIn("Per-phase VM readiness/capture timeout", result.stdout)
        self.assertIn("downloads/builds", result.stdout)
        self.assertIn("--iio-report", result.stdout)
        self.assertIn("--uart-port", result.stdout)
        self.assertIn("--gdb", result.stdout)
        self.assertIn("UART1 console", result.stdout)

    def test_runtime_manifest_distinguishes_custom_machine_from_stock_qemu(self) -> None:
        source = PREPARE.read_text()
        for token in (
            'manifest["execution_target"]',
            '"machine": "xilinx-zynq-a9,p210=on"',
            '"gem_phy_address": 0',
            '"four-entry AXI-DMAC"',
            '"USB gadget controller"',
            "CPU copy, PL-FFT DMA",
        ):
            self.assertIn(token, source)

    def test_complete_appliance_owns_firmware_and_usbip_lifecycles(self) -> None:
        source = APPLIANCE.read_text()
        for token in (
            '"$ROOT/scripts/run_p210_firmware.sh" --serve',
            "P210_RUNTIME READY",
            "python3 -m neptunesdr_twin usbip-serve",
            '--iiod-backend "127.0.0.1:$IIO_PORT"',
            "NEPTUNE_APPLIANCE READY",
            "trap cleanup 0",
            "busid=1-1",
            'uart=tcp:127.0.0.1:%s',
            '--uart-port "$UART_PORT"',
            '--gdb "$GDB_PORT"',
        ):
            self.assertIn(token, source)
        result = subprocess.run(
            [str(APPLIANCE), "--no-build", "--dry-run"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertIn("backend=127.0.0.1:30431", result.stdout)

    def test_acceptance_wrapper_includes_every_evidence_layer(self) -> None:
        source = ACCEPT.read_text()
        for token in (
            'unittest discover -s "$ROOT/tests"',
            'unittest discover -s "$ROOT/cosim/tests"',
            '"$ROOT/scripts/test_firmware.py" --fetch --json',
            "p210-system-xsa",
            "neptunesdr_twin appliance --dry-run",
            '"$ROOT/scripts/run_p210_firmware.sh"',
            "P210_RUNTIME PASS",
            "NEPTUNE_TWIN_ACCEPTANCE PASS",
        ):
            self.assertIn(token, source)


if __name__ == "__main__":
    unittest.main()
