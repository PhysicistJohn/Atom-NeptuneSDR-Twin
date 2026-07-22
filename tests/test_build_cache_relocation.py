"""Focused checks for the path-sensitive native build-cache boundary."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "cache_relocation.sh"
QEMU_BUILD = ROOT / "scripts" / "build_p210_qemu.sh"
HOST_LIBIIO_BUILD = ROOT / "scripts" / "build_host_libiio.sh"


def guard(cache: Path, *artifacts: str) -> subprocess.CompletedProcess[str]:
    quoted_cache = str(cache).replace("'", "'\\''")
    arguments = " ".join(f"'{item}'" for item in artifacts)
    command = (
        f". '{HELPER}'; "
        f"guard_relocatable_cache '{quoted_cache}' test-v1 cache-test {arguments} "
        "|| exit $?; "
        'printf "action=%s\\nreason=%s\\n" '
        '"$CACHE_RELOCATION_ACTION" "$CACHE_RELOCATION_REASON"'
    )
    return subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        text=True,
        capture_output=True,
    )


class RelocatableBuildCacheTests(unittest.TestCase):
    def test_guard_stamp_is_not_mistaken_for_an_unknown_toolchain(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "toolchain"
            self.assertEqual(guard(cache, "bin", "conda-meta").returncode, 0)
            quoted_helper = str(HELPER).replace("'", "'\\''")
            quoted_cache = str(cache).replace("'", "'\\''")
            command = (
                f". '{quoted_helper}'; "
                f"cache_has_only_relocation_stamp '{quoted_cache}'"
            )
            empty = subprocess.run(
                ["/bin/sh", "-c", command], check=False, capture_output=True
            )
            self.assertEqual(empty.returncode, 0)

            (cache / "unexpected-tool").write_text("unknown", encoding="utf-8")
            occupied = subprocess.run(
                ["/bin/sh", "-c", command], check=False, capture_output=True
            )
            self.assertNotEqual(occupied.returncode, 0)

    def test_conda_residue_is_removed_on_toolchain_schema_change(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "toolchain"
            self.assertEqual(guard(cache, "doc", ".mambarc").returncode, 0)
            (cache / "doc").mkdir()
            (cache / "doc" / "langref.html").write_text("old prefix")
            (cache / ".mambarc").write_text("old prefix")

            quoted_cache = str(cache).replace("'", "'\\''")
            command = (
                f". '{HELPER}'; "
                f"guard_relocatable_cache '{quoted_cache}' test-v2 cache-test "
                "doc .mambarc; "
                'printf "action=%s reason=%s\\n" '
                '"$CACHE_RELOCATION_ACTION" "$CACHE_RELOCATION_REASON"'
            )
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("action=invalidated reason=schema-change", result.stdout)
            self.assertFalse((cache / "doc").exists())
            self.assertFalse((cache / ".mambarc").exists())

    def test_external_prefix_creator_can_be_stamped_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "toolchain"
            self.assertEqual(guard(cache, "bin", "conda-meta").returncode, 0)
            (cache / ".neptune-cache-location").unlink()
            cache.rmdir()

            # Stand in for a prefix creator that refuses pre-existing paths.
            (cache / "conda-meta").mkdir(parents=True)
            marker = cache / "conda-meta" / "history"
            marker.write_text("created", encoding="utf-8")
            quoted_helper = str(HELPER).replace("'", "'\\''")
            quoted_cache = str(cache).replace("'", "'\\''")
            command = (
                f". '{quoted_helper}'; "
                f"stamp_relocatable_cache '{quoted_cache}' test-v1; "
                f"guard_relocatable_cache '{quoted_cache}' test-v1 cache-test "
                "bin conda-meta; "
                'printf "action=%s\\n" "$CACHE_RELOCATION_ACTION"'
            )
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("action=unchanged", result.stdout)
            self.assertEqual(marker.read_text(encoding="utf-8"), "created")

    def test_unchanged_cache_preserves_sensitive_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            self.assertEqual(guard(cache, "env", "build").returncode, 0)
            (cache / "env").mkdir()
            marker = cache / "env" / "still-here"
            marker.write_text("yes", encoding="utf-8")

            result = guard(cache, "env", "build")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("action=unchanged", result.stdout)
            self.assertTrue(marker.exists())

    def test_moved_cache_discards_only_path_sensitive_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            original = parent / "old-name"
            self.assertEqual(guard(original, "env", "build").returncode, 0)
            (original / "env").mkdir()
            (original / "build").mkdir()
            (original / "downloads").mkdir()
            retained = original / "downloads" / "qemu.tar.xz"
            retained.write_bytes(b"content-addressed")

            moved = parent / "new-name"
            original.rename(moved)
            result = guard(moved, "env", "build")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("action=invalidated", result.stdout)
            self.assertIn("reason=relocated", result.stdout)
            self.assertFalse((moved / "env").exists())
            self.assertFalse((moved / "build").exists())
            self.assertEqual(retained.name, "qemu.tar.xz")
            self.assertEqual(
                (moved / "downloads" / retained.name).read_bytes(),
                b"content-addressed",
            )
            stamp = (moved / ".neptune-cache-location").read_text(encoding="utf-8")
            self.assertIn(f"path={moved.resolve()}\n", stamp)

    def test_legacy_cache_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            (cache / "install").mkdir(parents=True)
            stale = cache / "install" / "absolute-prefix.pc"
            stale.write_text("prefix=/deleted/repository", encoding="utf-8")

            result = guard(cache, "build", "install")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reason=legacy", result.stdout)
            self.assertFalse(stale.exists())

    def test_guard_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.write_text("do not remove", encoding="utf-8")
            result = guard(Path(directory) / "cache", "../outside")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not remove")

    def test_qemu_entry_point_is_relative_and_verify_mode_exists(self):
        script = QEMU_BUILD.read_text(encoding="utf-8")
        self.assertIn('OUTPUT_LINK="../src/qemu-10.0.2/build-p210/qemu-system-arm"', script)
        self.assertIn('ln -sfn "$OUTPUT_LINK" "$OUTPUT"', script)
        self.assertIn("--verify", script)
        self.assertNotIn('ln -sfn "$BUILD/qemu-system-arm" "$OUTPUT"', script)

    def test_libiio_builder_has_relocation_safe_cmake_fallback(self):
        script = HOST_LIBIIO_BUILD.read_text(encoding="utf-8")
        self.assertIn('"$ROOT/.venv/bin/python" -m cmake --version', script)
        self.assertIn('"$CMAKE_BIN" -m cmake "$@"', script)
        self.assertNotIn('CMAKE_BIN=$(find_cmake)', script)

    def test_qemu_verify_rejects_absolute_link_and_accepts_relative_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "qemu-cache"
            target = cache / "src" / "qemu-10.0.2" / "build-p210" / "qemu-system-arm"
            target.parent.mkdir(parents=True)
            target.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo 'QEMU emulator version 10.0.2' ;;\n"
                "  -machine) echo 'p210=<bool> Enable HAMGEEK P210 SDR devices' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            entry = cache / "bin" / "qemu-system-arm"
            entry.parent.mkdir(parents=True)
            entry.symlink_to(target)
            (cache / ".neptune-cache-location").write_text(
                f"schema=qemu-p210-v1\npath={cache.resolve()}\n",
                encoding="utf-8",
            )

            stub_bin = root / "stub-bin"
            stub_bin.mkdir()
            uname = stub_bin / "uname"
            uname.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n",
                encoding="utf-8",
            )
            uname.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{stub_bin}{os.pathsep}{environment['PATH']}"
            environment["P210_QEMU_CACHE"] = str(cache)

            rejected = subprocess.run(
                [str(QEMU_BUILD), "--verify"],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("failed verification", rejected.stderr)

            entry.unlink()
            entry.symlink_to("../src/qemu-10.0.2/build-p210/qemu-system-arm")
            accepted = subprocess.run(
                [str(QEMU_BUILD), "--verify"],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("cache=verified", accepted.stdout)


if __name__ == "__main__":
    unittest.main()
