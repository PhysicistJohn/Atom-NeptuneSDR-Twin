"""Fail-closed tests for the Firmwave runtime-bundle trust boundary."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_firmwave_bundle.py"
SPEC = importlib.util.spec_from_file_location("verify_firmwave_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class BundleFixture:
    """A minimal clean Firmwave checkout and its self-describing runtime."""

    ARTIFACTS = {
        "kernel": ("p210-kernel.bin", b"fixture P210 kernel\n", "P210 Linux kernel"),
        "devicetree": (
            "p210-devicetree.dtb",
            b"fixture P210 device tree\n",
            "P210 device tree",
        ),
        "rootfs": (
            "pluto-v0.39-rootfs.cpio.gz",
            b"fixture Pluto rootfs\n",
            "released Pluto rootfs",
        ),
        "fft_runtime": (
            "qemu-fft-runtime.cpio.gz",
            b"fixture FFT runtime\n",
            "QEMU FFT development runtime",
        ),
    }

    def __init__(self, base: Path) -> None:
        self.firmwave_root = base / "Atom-NeptuneSDR_Firmwave"
        self.runtime = base / "runtime"
        self.lock_path = base / "firmwave.lock.json"
        self.firmwave_root.mkdir()
        self.runtime.mkdir()

        interface = self.firmwave_root / "specs" / "p210-firmware-interface-v1.json"
        interface.parent.mkdir()
        interface.write_text(
            json.dumps(
                {
                    "schema": verifier.INTERFACE_SCHEMA,
                    "profile": "qemu-development",
                    "flashable": False,
                    "produced_artifacts": {
                        "runtime_manifest_schema": verifier.RUNTIME_SCHEMA,
                        **{
                            name: relative
                            for name, (relative, _payload, _role) in self.ARTIFACTS.items()
                        },
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _git(self.firmwave_root, "init", "--quiet")
        _git(self.firmwave_root, "add", ".")
        _git(
            self.firmwave_root,
            "-c",
            "user.name=Firmwave Bundle Test",
            "-c",
            "user.email=bundle@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )

        current = verifier._source_state(self.firmwave_root)
        tree = _git(self.firmwave_root, "rev-parse", "HEAD^{tree}")
        self.source = {
            "repository": "Atom-NeptuneSDR_Firmwave",
            "commit": current["commit"],
            "tree": tree,
            "state_sha256": current["state_sha256"],
            "clean": True,
        }
        interface_path = interface.relative_to(self.firmwave_root).as_posix()
        interface_sha = _sha256(interface)
        self.lock = {
            "schema_version": 1,
            "profile": "qemu-development",
            "repository": {
                "url": "https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave.git",
                "commit": current["commit"],
                "tree": tree,
            },
            "interface": {
                "path": interface_path,
                "sha256": interface_sha,
            },
        }
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

        generated = {}
        for name, (relative, payload, role) in self.ARTIFACTS.items():
            artifact = self.runtime / relative
            artifact.write_bytes(payload)
            generated[name] = {
                "path": relative,
                "bytes": len(payload),
                "sha256": _sha256(artifact),
                "role": role,
            }
        self.manifest = {
            "schema": verifier.RUNTIME_SCHEMA,
            "profile": "qemu-development",
            "flashable": False,
            "artifact_hashes_complete": True,
            "firmwave_source": self.source,
            "interface": {
                "schema": verifier.INTERFACE_SCHEMA,
                "path": interface_path,
                "sha256": interface_sha,
            },
            "generated_artifacts": generated,
        }
        self.write_manifest()

    @property
    def manifest_path(self) -> Path:
        return self.runtime / "runtime-manifest.json"

    def write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def verify(self):
        return verifier.verify_bundle(
            self.firmwave_root,
            self.runtime,
            self.lock_path,
        )


class FirmwaveBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="firmwave-bundle-")
        self.fixture = BundleFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_bundle_binds_source_interface_and_every_artifact(self) -> None:
        result = self.fixture.verify()

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["profile"], "qemu-development")
        self.assertIs(result["flashable"], False)
        self.assertEqual(set(result["artifacts"]), set(BundleFixture.ARTIFACTS))
        self.assertEqual(
            result["interface"]["sha256"],
            self.fixture.lock["interface"]["sha256"],
        )

    def test_dirty_firmwave_source_is_rejected(self) -> None:
        (self.fixture.firmwave_root / "untracked-work.txt").write_text(
            "must not enter evidence\n", encoding="utf-8"
        )
        dirty = verifier._source_state(self.fixture.firmwave_root)
        self.fixture.manifest["firmwave_source"]["state_sha256"] = dirty[
            "state_sha256"
        ]
        self.fixture.write_manifest()

        with self.assertRaisesRegex(ValueError, "checkout is dirty"):
            self.fixture.verify()

    def test_unsafe_artifact_path_is_rejected(self) -> None:
        self.fixture.manifest["generated_artifacts"]["kernel"]["path"] = (
            "../escaped-kernel.bin"
        )
        self.fixture.write_manifest()

        with self.assertRaisesRegex(ValueError, "not a safe relative path"):
            self.fixture.verify()

    def test_artifact_tamper_is_rejected(self) -> None:
        kernel = self.fixture.runtime / BundleFixture.ARTIFACTS["kernel"][0]
        kernel.write_bytes(kernel.read_bytes() + b"tampered\n")

        with self.assertRaisesRegex(ValueError, "artifact hash/size mismatch: kernel"):
            self.fixture.verify()

    def test_hashed_decoy_cannot_substitute_for_canonical_boot_kernel(self) -> None:
        canonical = self.fixture.runtime / BundleFixture.ARTIFACTS["kernel"][0]
        decoy = self.fixture.runtime / "decoy-kernel.bin"
        decoy.write_bytes(b"innocent decoy kernel\n")
        self.assertNotEqual(canonical.read_bytes(), decoy.read_bytes())
        kernel_record = self.fixture.manifest["generated_artifacts"]["kernel"]
        kernel_record.update(
            {
                "path": decoy.name,
                "bytes": decoy.stat().st_size,
                "sha256": _sha256(decoy),
            }
        )
        self.fixture.write_manifest()

        with self.assertRaisesRegex(
            ValueError, "kernel path does not match the locked interface"
        ):
            self.fixture.verify()

    def test_wrong_profile_flashable_or_interface_hash_is_rejected(self) -> None:
        original = copy.deepcopy(self.fixture.manifest)
        cases = (
            ("profile", lambda value: value.__setitem__("profile", "hardware"), "profile"),
            ("flashable", lambda value: value.__setitem__("flashable", True), "non-flashable"),
            (
                "interface hash",
                lambda value: value["interface"].__setitem__("sha256", "0" * 64),
                "manifest interface hash is invalid",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(case=label):
                self.fixture.manifest = copy.deepcopy(original)
                mutate(self.fixture.manifest)
                self.fixture.write_manifest()
                with self.assertRaisesRegex(ValueError, message):
                    self.fixture.verify()

    def test_missing_required_artifact_is_rejected(self) -> None:
        del self.fixture.manifest["generated_artifacts"]["rootfs"]
        self.fixture.write_manifest()

        with self.assertRaisesRegex(ValueError, "lacks required artifacts: rootfs"):
            self.fixture.verify()

    def test_duplicate_artifact_path_is_rejected(self) -> None:
        generated = self.fixture.manifest["generated_artifacts"]
        generated["kernel_copy"] = copy.deepcopy(generated["kernel"])
        self.fixture.write_manifest()

        with self.assertRaisesRegex(ValueError, "generated artifacts reuse path"):
            self.fixture.verify()


if __name__ == "__main__":
    unittest.main()
