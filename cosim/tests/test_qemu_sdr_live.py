"""Live qtests for the P210 AXI-DMAC firmware contract."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from typing import Optional
import unittest


ROOT = Path(__file__).resolve().parents[2]
RX_CORE = 0x79020000
RX_DMAC = 0x7C400000
TX_DMAC = 0x7C420000
CPU_RST_CTRL = 0xF8000244

IRQ_MASK = 0x080
IRQ_PENDING = 0x084
IRQ_SOURCE = 0x088
CONTROL = 0x400
TRANSFER_ID = 0x404
START_TRANSFER = 0x408
FLAGS = 0x40C
DEST_ADDRESS = 0x410
SRC_ADDRESS = 0x414
X_LENGTH = 0x418
Y_LENGTH = 0x41C
DEST_STRIDE = 0x420
SRC_STRIDE = 0x424
TRANSFER_DONE = 0x428
ACTIVE_TRANSFER_ID = 0x42C
STATUS = 0x430
CURRENT_DEST_ADDRESS = 0x434
CURRENT_SRC_ADDRESS = 0x438


def _qemu_binary() -> Path:
    override = os.environ.get("P210_QEMU_BINARY")
    if override:
        return Path(override)
    return ROOT / ".cache/qemu-p210/src/qemu-10.0.2/build-p210/qemu-system-arm"


class _QTest:
    def __init__(self, *extra_args: str) -> None:
        self.extra_args = extra_args

    def __enter__(self) -> "_QTest":
        binary = _qemu_binary()
        if not binary.is_file():
            raise unittest.SkipTest(
                "set P210_QEMU_BINARY to an integrated P210 QEMU binary"
            )
        self.process = subprocess.Popen(
            [
                str(binary),
                "-machine",
                "xilinx-zynq-a9,p210=on",
                "-accel",
                "qtest",
                "-display",
                "none",
                "-nodefaults",
                "-qtest",
                "stdio",
                *self.extra_args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if self.readl(RX_DMAC) != 0x00040061:
            self.close()
            raise unittest.SkipTest("QEMU binary does not contain p210-sdr")
        return self

    def command(self, command: str, *, require_ok: bool = True) -> str:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline().strip()
        if require_ok and not response.startswith("OK"):
            raise AssertionError(f"qtest command failed: {command} -> {response}")
        return response

    def readl(self, address: int) -> int:
        return int(self.command(f"readl 0x{address:x}")[2:].strip(), 0)

    def writel(self, address: int, value: int) -> None:
        self.command(f"writel 0x{address:x} 0x{value:x}")

    def write(self, address: int, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        self.command(f"b64write 0x{address:x} 0x{len(data):x} {encoded}")

    def read(self, address: int, size: int) -> bytes:
        encoded = self.command(f"b64read 0x{address:x} 0x{size:x}")[2:].strip()
        return base64.b64decode(encoded)

    def clock_step(self, nanoseconds: Optional[int] = None) -> int:
        command = "clock_step"
        if nanoseconds is not None:
            command += f" 0x{nanoseconds:x}"
        return int(self.command(command)[2:].strip(), 0)

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _QMP:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sequence = 0

    def __enter__(self) -> "_QMP":
        deadline = time.monotonic() + 5
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        while True:
            try:
                self.socket.connect(str(self.path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        self.file = self.socket.makefile("rwb")
        greeting = self._read_message()
        if "QMP" not in greeting:
            raise AssertionError(f"invalid QMP greeting: {greeting}")
        self.execute("qmp_capabilities")
        return self

    def _read_message(self) -> dict[str, object]:
        line = self.file.readline()
        if not line:
            raise AssertionError("QMP connection closed")
        return json.loads(line)

    def execute(
        self, command: str, arguments: Optional[dict[str, object]] = None
    ) -> object:
        self.sequence += 1
        request: dict[str, object] = {
            "execute": command,
            "id": self.sequence,
        }
        if arguments is not None:
            request["arguments"] = arguments
        self.file.write(json.dumps(request).encode("utf-8") + b"\n")
        self.file.flush()
        while True:
            response = self._read_message()
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise AssertionError(f"QMP command failed: {response['error']}")
            return response.get("return")

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.file.close()
        self.socket.close()


def _enable_scan(qtest: _QTest, mask: int) -> None:
    for channel in range(4):
        qtest.writel(
            RX_CORE + 0x400 + channel * 0x40,
            1 if mask & (1 << channel) else 0,
        )


def _submit_rx(
    qtest: _QTest,
    *,
    destination: int,
    x_length: int,
    y_length: int = 0,
    stride: int = 0,
) -> None:
    qtest.writel(RX_DMAC + DEST_ADDRESS, destination)
    qtest.writel(RX_DMAC + X_LENGTH, x_length)
    qtest.writel(RX_DMAC + Y_LENGTH, y_length)
    qtest.writel(RX_DMAC + DEST_STRIDE, stride)
    qtest.writel(RX_DMAC + START_TRANSFER, 1)


def _submit_tx(qtest: _QTest, *, source: int, x_length: int) -> None:
    qtest.writel(TX_DMAC + SRC_ADDRESS, source)
    qtest.writel(TX_DMAC + X_LENGTH, x_length)
    qtest.writel(TX_DMAC + START_TRANSFER, 1)


class P210SDRLiveTests(unittest.TestCase):
    def test_irq_pending_is_masked_view_and_source_is_raw(self) -> None:
        with _QTest() as qtest:
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_MASK), 0x3)
            qtest.writel(RX_DMAC + CONTROL, 1)
            _submit_rx(
                qtest, destination=0x01000000, x_length=7
            )
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 0)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 1)
            qtest.writel(RX_DMAC + IRQ_SOURCE, 1)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 1)

            qtest.writel(RX_DMAC + IRQ_MASK, 0)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 1)
            qtest.writel(RX_DMAC + IRQ_PENDING, 1)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 0)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 0)
            qtest.writel(RX_DMAC + CONTROL, 0)

    def test_submit_while_disabled_is_ignored(self) -> None:
        with _QTest() as qtest:
            qtest.writel(RX_DMAC + START_TRANSFER, 1)
            self.assertEqual(qtest.readl(RX_DMAC + START_TRANSFER), 0)
            self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_ID), 0)
            self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 0)

    def test_xsa_capabilities_alignment_and_pause_readback(self) -> None:
        with _QTest() as qtest:
            self.assertEqual(qtest.readl(RX_DMAC + FLAGS), 0x2)
            self.assertEqual(qtest.readl(TX_DMAC + FLAGS), 0x3)
            qtest.writel(RX_DMAC + X_LENGTH, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(RX_DMAC + X_LENGTH), 0x00FFFFFF)
            for register in (Y_LENGTH, DEST_STRIDE, SRC_STRIDE):
                qtest.writel(RX_DMAC + register, 0xFFFFFFFF)
                self.assertEqual(qtest.readl(RX_DMAC + register), 0)
            qtest.writel(RX_DMAC + DEST_ADDRESS, 0x01000007)
            self.assertEqual(qtest.readl(RX_DMAC + DEST_ADDRESS), 0x01000000)
            qtest.writel(RX_DMAC + DEST_ADDRESS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(RX_DMAC + DEST_ADDRESS), 0x1FFFFFF8)
            qtest.writel(RX_DMAC + SRC_ADDRESS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(RX_DMAC + SRC_ADDRESS), 0)
            qtest.writel(TX_DMAC + DEST_ADDRESS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(TX_DMAC + DEST_ADDRESS), 0)
            qtest.writel(TX_DMAC + SRC_ADDRESS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(TX_DMAC + SRC_ADDRESS), 0x1FFFFFF8)
            qtest.writel(RX_DMAC + FLAGS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(RX_DMAC + FLAGS), 0x2)
            qtest.writel(TX_DMAC + FLAGS, 0xFFFFFFFF)
            self.assertEqual(qtest.readl(TX_DMAC + FLAGS), 0x3)
            qtest.writel(RX_DMAC + CONTROL, 0x3)
            self.assertEqual(qtest.readl(RX_DMAC + CONTROL), 0x3)

    def test_unsupported_two_dimensional_fields_do_not_expand_transfer(self) -> None:
        destination = 0x01000000
        sentinel = bytes([0xA5]) * 32

        with _QTest() as qtest:
            _enable_scan(qtest, 0xF)
            qtest.write(destination, sentinel)
            qtest.writel(RX_DMAC + CONTROL, 1)
            _submit_rx(
                qtest,
                destination=destination,
                x_length=7,
                y_length=1,
                stride=16,
            )
            self.assertEqual(qtest.clock_step(), 17)
            self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 1)

            data = qtest.read(destination, len(sentinel))
            self.assertNotEqual(data[0:8], sentinel[0:8])
            self.assertEqual(data[8:], sentinel[8:])
            self.assertEqual(
                qtest.readl(RX_DMAC + CURRENT_DEST_ADDRESS), destination + 8
            )
            self.assertEqual(qtest.readl(RX_DMAC + CURRENT_SRC_ADDRESS), 0)

    def test_tx_cyclic_suppresses_ids_done_and_interrupts(self) -> None:
        source = 0x01000000
        with _QTest() as qtest:
            qtest.write(source, bytes(range(8)))
            qtest.writel(TX_DMAC + CONTROL, 1)
            qtest.writel(TX_DMAC + FLAGS, 1)
            _submit_tx(qtest, source=source, x_length=7)

            self.assertEqual(qtest.readl(TX_DMAC + START_TRANSFER), 0)
            self.assertEqual(qtest.readl(TX_DMAC + TRANSFER_ID), 0)
            self.assertEqual(qtest.readl(TX_DMAC + ACTIVE_TRANSFER_ID), 0)
            self.assertEqual(qtest.readl(TX_DMAC + TRANSFER_DONE), 0)
            self.assertEqual(qtest.readl(TX_DMAC + IRQ_SOURCE), 0)

            self.assertEqual(qtest.clock_step(), 17)
            self.assertEqual(qtest.readl(TX_DMAC + TRANSFER_ID), 0)
            self.assertEqual(qtest.readl(TX_DMAC + TRANSFER_DONE), 0)
            self.assertEqual(qtest.readl(TX_DMAC + IRQ_SOURCE), 0)
            self.assertEqual(qtest.readl(TX_DMAC + CURRENT_DEST_ADDRESS), 0)
            self.assertEqual(
                qtest.readl(TX_DMAC + CURRENT_SRC_ADDRESS), source + 8
            )
            self.assertEqual(qtest.clock_step(), 34)
            qtest.writel(TX_DMAC + CONTROL, 0)

    def test_three_channel_scan_is_contiguous_across_copy_chunks(self) -> None:
        sine_q15 = (
            0, 3212, 6393, 9512, 12539, 15446, 18204, 20787,
            23170, 25329, 27245, 28898, 30273, 31356, 32137, 32609,
            32767, 32609, 32137, 31356, 30273, 28898, 27245, 25329,
            23170, 20787, 18204, 15446, 12539, 9512, 6393, 3212,
            0, -3212, -6393, -9512, -12539, -15446, -18204, -20787,
            -23170, -25329, -27245, -28898, -30273, -31356, -32137,
            -32609, -32767, -32609, -32137, -31356, -30273, -28898,
            -27245, -25329, -23170, -20787, -18204, -15446, -12539,
            -9512, -6393, -3212,
        )
        frames = 1024
        destination = 0x01000000

        expected = bytearray()
        for sample_index in range(frames):
            for step, phase, quadrature, amplitude in (
                (5, 0, False, 1536),
                (5, 0, True, 1536),
                (13, 8, False, 1024),
            ):
                index = (
                    sample_index * step + phase + (0 if quadrature else 16)
                ) & 63
                value = int(sine_q15[index] * amplitude / 32767)
                expected.extend((value & 0xFFFF).to_bytes(2, "little"))

        with _QTest() as qtest:
            _enable_scan(qtest, 0x7)
            qtest.writel(RX_DMAC + CONTROL, 1)
            _submit_rx(
                qtest,
                destination=destination,
                x_length=len(expected) - 1,
            )
            qtest.clock_step()
            self.assertEqual(qtest.read(destination, len(expected)), expected)

    def test_pause_suspends_and_resume_completes(self) -> None:
        with _QTest() as qtest:
            _enable_scan(qtest, 0xF)
            qtest.writel(RX_DMAC + CONTROL, 0x3)
            _submit_rx(qtest, destination=0x01000000, x_length=7)
            response = qtest.command("clock_step", require_ok=False)
            self.assertIn("no pending deadline", response)
            self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 0)

            qtest.writel(RX_DMAC + CONTROL, 1)
            self.assertEqual(qtest.clock_step(), 17)
            self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 1)

    def test_dma_decode_error_uses_standard_completion_and_qemu_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p210-dma-error-") as temporary:
            log = Path(temporary) / "qemu.log"
            with _QTest("-d", "guest_errors", "-D", str(log)) as qtest:
                _enable_scan(qtest, 0xF)
                qtest.writel(RX_DMAC + CONTROL, 1)
                _submit_rx(qtest, destination=0x1F000000, x_length=7)
                self.assertEqual(qtest.clock_step(), 17)
                self.assertEqual(qtest.readl(RX_DMAC + STATUS), 0)
                self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 1)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 0)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 0x3)

                _submit_rx(qtest, destination=0x01000000, x_length=7)
                self.assertEqual(qtest.clock_step(), 34)
                self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 3)
                self.assertEqual(qtest.readl(RX_DMAC + STATUS), 0)

            self.assertIn("DMA address error at 0x1f000000", log.read_text())

    def test_system_reset_cancels_timer_queue_and_irq_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p210-reset-") as temporary:
            qmp_path = Path(temporary) / "qmp"
            with _QTest(
                "-qmp", f"unix:{qmp_path},server=on,wait=off"
            ) as qtest, _QMP(qmp_path) as qmp:
                _enable_scan(qtest, 0xF)
                qtest.writel(RX_DMAC + IRQ_MASK, 0)
                qtest.writel(RX_DMAC + CONTROL, 0x3)
                _submit_rx(qtest, destination=0x01000000, x_length=7)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 1)

                qmp.execute("system_reset")
                self.assertEqual(qtest.readl(RX_DMAC + CONTROL), 0)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_MASK), 3)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_PENDING), 0)
                self.assertEqual(qtest.readl(RX_DMAC + IRQ_SOURCE), 0)
                self.assertEqual(qtest.readl(RX_DMAC + TRANSFER_DONE), 0)
                self.assertEqual(qtest.readl(RX_DMAC + FLAGS), 2)
                self.assertEqual(qtest.readl(TX_DMAC + FLAGS), 3)
                response = qtest.command("clock_step", require_ok=False)
                self.assertIn("no pending deadline", response)

    def test_paused_inflight_queue_and_deadline_survive_migration(self) -> None:
        first_destination = 0x01000000
        second_destination = 0x01000020
        sentinel = bytes([0xA5]) * 8

        with tempfile.TemporaryDirectory(prefix="p210-migration-") as temporary:
            directory = Path(temporary)
            qmp_path = directory / "source.qmp"
            destination_qmp_path = directory / "destination.qmp"
            migration = directory / "state"

            with _QTest(
                "-qmp", f"unix:{qmp_path},server=on,wait=off"
            ) as source:
                _enable_scan(source, 0xF)
                source.write(first_destination, sentinel)
                source.write(second_destination, sentinel)
                source.writel(RX_DMAC + CONTROL, 1)
                _submit_rx(
                    source, destination=first_destination, x_length=7
                )
                self.assertEqual(source.clock_step(8), 8)
                source.writel(RX_DMAC + CONTROL, 0x3)
                _submit_rx(
                    source, destination=second_destination, x_length=7
                )

                with _QMP(qmp_path) as qmp:
                    qmp.execute("migrate", {"uri": f"file:{migration}"})
                    deadline = time.monotonic() + 5
                    while True:
                        status = qmp.execute("query-migrate")
                        assert isinstance(status, dict)
                        state = status.get("status")
                        if state == "completed":
                            break
                        if state in {"failed", "cancelled"}:
                            self.fail(f"migration did not complete: {status}")
                        if time.monotonic() >= deadline:
                            self.fail(f"migration timed out: {status}")
                        time.sleep(0.01)

            with _QTest(
                "-qmp",
                f"unix:{destination_qmp_path},server=on,wait=off",
                "-incoming",
                f"file:{migration}",
            ) as destination, _QMP(destination_qmp_path) as destination_qmp:
                deadline = time.monotonic() + 5
                while True:
                    status = destination_qmp.execute("query-migrate")
                    assert isinstance(status, dict)
                    state = status.get("status")
                    if state == "completed":
                        break
                    if state in {"failed", "cancelled"}:
                        self.fail(f"incoming migration did not complete: {status}")
                    if time.monotonic() >= deadline:
                        self.fail(f"incoming migration timed out: {status}")
                    time.sleep(0.01)

                self.assertEqual(destination.readl(RX_DMAC + CONTROL), 0x3)
                self.assertEqual(destination.readl(RX_DMAC + TRANSFER_DONE), 0)
                response = destination.command("clock_step", require_ok=False)
                self.assertIn("no pending deadline", response)

                destination.writel(RX_DMAC + CONTROL, 1)
                self.assertEqual(destination.clock_step(), 9)
                self.assertEqual(destination.readl(RX_DMAC + TRANSFER_DONE), 1)
                self.assertNotEqual(destination.read(first_destination, 8), sentinel)

                self.assertEqual(destination.clock_step(), 26)
                self.assertEqual(destination.readl(RX_DMAC + TRANSFER_DONE), 3)
                self.assertNotEqual(destination.read(second_destination, 8), sentinel)

    def test_cpu1_reset_control_survives_migration_and_system_reset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p210-cpu-reset-") as temporary:
            directory = Path(temporary)
            source_qmp_path = directory / "source.qmp"
            destination_qmp_path = directory / "destination.qmp"
            migration = directory / "state"

            with _QTest(
                "-smp", "2",
                "-qmp", f"unix:{source_qmp_path},server=on,wait=off",
            ) as source, _QMP(source_qmp_path) as source_qmp:
                source.writel(CPU_RST_CTRL, 0x22)
                self.assertEqual(source.readl(CPU_RST_CTRL), 0x22)
                source_qmp.execute("migrate", {"uri": f"file:{migration}"})
                deadline = time.monotonic() + 5
                while True:
                    status = source_qmp.execute("query-migrate")
                    assert isinstance(status, dict)
                    state = status.get("status")
                    if state == "completed":
                        break
                    if state in {"failed", "cancelled"}:
                        self.fail(f"migration did not complete: {status}")
                    if time.monotonic() >= deadline:
                        self.fail(f"migration timed out: {status}")
                    time.sleep(0.01)

            with _QTest(
                "-smp", "2",
                "-qmp", f"unix:{destination_qmp_path},server=on,wait=off",
                "-incoming", f"file:{migration}",
            ) as destination, _QMP(destination_qmp_path) as destination_qmp:
                deadline = time.monotonic() + 5
                while True:
                    status = destination_qmp.execute("query-migrate")
                    assert isinstance(status, dict)
                    state = status.get("status")
                    if state == "completed":
                        break
                    if state in {"failed", "cancelled"}:
                        self.fail(f"incoming migration did not complete: {status}")
                    if time.monotonic() >= deadline:
                        self.fail(f"incoming migration timed out: {status}")
                    time.sleep(0.01)

                self.assertEqual(destination.readl(CPU_RST_CTRL), 0x22)
                destination_qmp.execute("system_reset")
                self.assertEqual(destination.readl(CPU_RST_CTRL), 0)


if __name__ == "__main__":
    unittest.main()
