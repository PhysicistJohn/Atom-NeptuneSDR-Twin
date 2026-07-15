import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from neptunesdr_twin.ad9361 import AD9361
from neptunesdr_twin.iio import IIOContext, IIODServer


REPOSITORY = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPOSITORY / "scripts" / "build_host_libiio.sh"
RUN_SCRIPT = REPOSITORY / "scripts" / "host_iio.sh"


def _fake_tool(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "printf 'argv='\n"
        "printf '<%s>' \"$@\"\n"
        "printf '\\nDYLD_LIBRARY_PATH=%s\\n' \"${DYLD_LIBRARY_PATH:-}\"\n"
        "printf 'LD_LIBRARY_PATH=%s\\n' \"${LD_LIBRARY_PATH:-}\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class HostLibiioScriptTests(unittest.TestCase):
    def test_build_lock_matches_runtime_evidence(self):
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        runtime_lock = json.loads(
            (REPOSITORY / "src/neptunesdr_twin/data/runtime-lock.json").read_text(
                encoding="utf-8"
            )
        )["upstream_runtime_source"]
        self.assertIn("LIBIIO_TAG=%s" % runtime_lock["libiio_tag"], text)
        self.assertIn("LIBIIO_COMMIT=%s" % runtime_lock["libiio_commit"], text)
        self.assertIn("LIBIIO_TREE=d35513bc71252029f769a85a021ba8a858560246", text)
        self.assertIn("-DWITH_NETWORK_BACKEND=ON", text)
        self.assertIn("-DWITH_USB_BACKEND=OFF", text)
        self.assertIn("-DWITH_LOCAL_BACKEND=OFF", text)

    def test_build_print_prefix_is_repo_local_and_overrideable(self):
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary) / "prefix"
            result = subprocess.run(
                [str(BUILD_SCRIPT), "--print-prefix"],
                env={**os.environ, "HOST_LIBIIO_PREFIX": str(expected)},
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(result.stdout.strip(), str(expected))

    def test_info_uses_forwarded_30431_and_repo_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "bin").mkdir()
            (prefix / "lib").mkdir()
            _fake_tool(prefix / "bin" / "iio_info")
            result = subprocess.run(
                [str(RUN_SCRIPT), "--prefix", str(prefix), "info", "-T", "2500"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output = result.stdout
            self.assertIn("argv=<-u><ip:127.0.0.1:30431><-T><2500>", output)
            if subprocess.run(["uname", "-s"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip() == "Darwin":
                # macOS strips DYLD_* when the fake script's protected /bin/sh
                # interpreter starts.  The real target is a directly exec'd
                # Mach-O binary; assert the wrapper exports the loader path.
                wrapper = RUN_SCRIPT.read_text(encoding="utf-8")
                self.assertIn("export DYLD_LIBRARY_PATH", wrapper)
            else:
                self.assertIn("LD_LIBRARY_PATH=%s" % (prefix / "lib"), output)

    def test_read_preserves_binary_tool_arguments_and_custom_uri(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "bin").mkdir()
            _fake_tool(prefix / "bin" / "iio_readdev")
            result = subprocess.run(
                [
                    str(RUN_SCRIPT),
                    "--prefix",
                    str(prefix),
                    "--uri",
                    "ip:localhost:40000",
                    "read",
                    "-b",
                    "4096",
                    "-s",
                    "32",
                    "cf-ad9361-lpc",
                    "voltage0",
                    "voltage1",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertIn(
            "argv=<-u><ip:localhost:40000><-b><4096><-s><32>"
            "<cf-ad9361-lpc><voltage0><voltage1>",
            result.stdout,
        )

    def test_missing_install_fails_with_build_instruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [str(RUN_SCRIPT), "--prefix", temporary, "version"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run scripts/build_host_libiio.sh", result.stderr)

    def test_built_upstream_clients_interoperate_when_present(self):
        prefix = Path(
            os.environ.get(
                "HOST_LIBIIO_PREFIX",
                REPOSITORY / ".cache/host-libiio-v0.26/install",
            )
        )
        if not (prefix / "bin/iio_info").is_file():
            self.skipTest("run scripts/build_host_libiio.sh for the native integration test")

        radio = AD9361()
        context = IIOContext(
            radio,
            rx_provider=lambda length: bytes(index & 0xFF for index in range(length)),
            tx_consumer=lambda data: None,
        )
        with IIODServer(context, port=0) as server:
            uri = "ip:%s:%d" % server.address
            info = subprocess.run(
                [
                    str(RUN_SCRIPT),
                    "--prefix",
                    str(prefix),
                    "--uri",
                    uri,
                    "info",
                    "-T",
                    "2000",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            capture = subprocess.run(
                [
                    str(RUN_SCRIPT),
                    "--prefix",
                    str(prefix),
                    "--uri",
                    uri,
                    "read",
                    "-T",
                    "2000",
                    "-b",
                    "32",
                    "-s",
                    "32",
                    "cf-ad9361-lpc",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertIn("iio_info version: 0.26 (git tag:a0eca0d)", info.stdout)
        self.assertIn("IIO context has 4 devices", info.stdout)
        self.assertIn("cf-ad9361-lpc (buffer capable)", info.stdout)
        self.assertEqual(len(capture.stdout), 32 * 8)
        self.assertEqual(capture.stdout, bytes(range(256)))


if __name__ == "__main__":
    unittest.main()
