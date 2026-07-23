"""Trust-boundary tests for the locked cross-repository Firmware dependency."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "resolve_firmware.py"
SPEC = importlib.util.spec_from_file_location("resolve_firmware", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
resolver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Firmware Resolver Test",
            "GIT_AUTHOR_EMAIL": "resolver@example.invalid",
            "GIT_COMMITTER_NAME": "Firmware Resolver Test",
            "GIT_COMMITTER_EMAIL": "resolver@example.invalid",
        },
    )
    return result.stdout.strip()


class Fixture:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.remote = base / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "--quiet")
        interface = self.remote / "specs" / "twin-interface.json"
        interface.parent.mkdir()
        interface.write_text('{"schema":"neptune-firmware-twin-v1"}\n', encoding="utf-8")
        payload = self.remote / "firmware" / "runtime.txt"
        payload.parent.mkdir()
        payload.write_text("trusted runtime\n", encoding="utf-8")
        git(self.remote, "add", ".")
        git(self.remote, "commit", "--quiet", "-m", "interface")
        self.commit = git(self.remote, "rev-parse", "HEAD")
        self.tree = git(self.remote, "rev-parse", "HEAD^{tree}")
        self.interface_sha = hashlib.sha256(interface.read_bytes()).hexdigest()
        self.lock_path = base / "firmware.lock.json"
        self.write_lock()

    def write_lock(self, **changes: object) -> None:
        payload = {
            "schema_version": 1,
            "profile": "qemu-development",
            "repository": {
                "url": str(self.remote),
                "commit": self.commit,
                "tree": self.tree,
            },
            "interface": {
                "path": "specs/twin-interface.json",
                "sha256": self.interface_sha,
            },
        }
        for dotted, value in changes.items():
            if "__" in dotted:
                group, field = dotted.split("__", 1)
                payload[group][field] = value  # type: ignore[index]
            else:
                payload[dotted] = value
        self.lock_path.write_text(json.dumps(payload), encoding="utf-8")

    def clone(self, name: str = "checkout") -> Path:
        checkout = self.base / name
        subprocess.run(
            ("git", "clone", "--quiet", str(self.remote), str(checkout)), check=True
        )
        return checkout


class FirmwareDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.fixture = Fixture(self.base)
        self.twin_root = self.base / "workspace" / "Atom-NeptuneSDR-Twin"
        self.twin_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def resolve(self, **kwargs: object):
        arguments = {
            "repo_root": self.twin_root,
            "lock_path": self.fixture.lock_path,
            "environment": {},
            "offline": True,
        }
        arguments.update(kwargs)
        return resolver.resolve_firmware(**arguments)

    def test_explicit_checkout_resolves_exact_release_identity(self):
        checkout = self.fixture.clone()
        result = self.resolve(explicit_root=checkout)
        self.assertEqual(result.source, "explicit")
        self.assertEqual(result.head_commit, self.fixture.commit)
        self.assertEqual(result.head_tree, self.fixture.tree)
        self.assertTrue(result.clean)
        self.assertTrue(result.release_ready)
        self.assertEqual(result.non_release_overrides, ())

    def test_environment_precedes_sibling(self):
        checkout = self.fixture.clone("environment")
        result = self.resolve(
            environment={"NEPTUNESDR_FIRMWARE_ROOT": str(checkout)}
        )
        self.assertEqual(result.source, "environment")
        self.assertEqual(Path(result.resolved_root), checkout.resolve())

    def test_explicit_mismatch_fails_without_falling_back_or_mutating(self):
        good = self.fixture.clone("good")
        bad = self.fixture.clone("bad")
        (bad / "later.txt").write_text("later\n", encoding="utf-8")
        git(bad, "add", ".")
        git(bad, "commit", "--quiet", "-m", "later")
        before = git(bad, "rev-parse", "HEAD")
        with self.assertRaisesRegex(resolver.ResolutionError, "commit mismatch"):
            self.resolve(
                explicit_root=bad,
                environment={"NEPTUNESDR_FIRMWARE_ROOT": str(good)},
                offline=False,
            )
        self.assertEqual(git(bad, "rev-parse", "HEAD"), before)

    def test_dirty_checkout_is_rejected_and_never_cleaned(self):
        checkout = self.fixture.clone()
        marker = checkout / "local-work.txt"
        marker.write_text("do not delete\n", encoding="utf-8")
        with self.assertRaisesRegex(resolver.ResolutionError, "checkout is dirty"):
            self.resolve(explicit_root=checkout)
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not delete\n")

    def test_allow_dirty_is_explicitly_non_release(self):
        checkout = self.fixture.clone()
        (checkout / "local-work.txt").write_text("work\n", encoding="utf-8")
        result = self.resolve(explicit_root=checkout, allow_dirty=True)
        self.assertFalse(result.clean)
        self.assertFalse(result.release_ready)
        self.assertEqual(result.non_release_overrides, ("allow-dirty",))

    def test_tree_mismatch_is_rejected_even_when_commit_matches(self):
        checkout = self.fixture.clone()
        self.fixture.write_lock(repository__tree="0" * len(self.fixture.tree))
        with self.assertRaisesRegex(resolver.ResolutionError, "tree mismatch"):
            self.resolve(explicit_root=checkout)

    def test_interface_digest_mismatch_is_rejected(self):
        checkout = self.fixture.clone()
        self.fixture.write_lock(interface__sha256="0" * 64)
        with self.assertRaisesRegex(resolver.ResolutionError, "SHA-256 mismatch"):
            self.resolve(explicit_root=checkout)

    def test_remote_identity_mismatch_is_rejected(self):
        checkout = self.fixture.clone()
        other = self.base / "other-remote"
        other.mkdir()
        self.fixture.write_lock(repository__url=str(other))
        with self.assertRaisesRegex(resolver.ResolutionError, "origin mismatch"):
            self.resolve(explicit_root=checkout)

    def test_canonical_github_remote_forms_match(self):
        left = resolver.canonical_remote("https://github.com/Owner/Repo.git")
        right = resolver.canonical_remote("git@github.com:Owner/Repo.git")
        self.assertEqual(left, right)

    def test_path_traversal_and_noncanonical_paths_are_rejected(self):
        for invalid in ("../interface.json", "/tmp/interface.json", "specs/../x", "specs\\x"):
            with self.subTest(path=invalid):
                self.fixture.write_lock(interface__path=invalid)
                with self.assertRaises(resolver.ResolutionError):
                    resolver.load_lock(self.fixture.lock_path)

    def test_lock_profile_is_required_and_exact(self):
        document = json.loads(self.fixture.lock_path.read_text(encoding="utf-8"))
        del document["profile"]
        self.fixture.lock_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(resolver.ResolutionError, "profile"):
            resolver.load_lock(self.fixture.lock_path)

        for invalid in (None, "", "production", "QEMU-DEVELOPMENT", 1):
            with self.subTest(profile=invalid):
                self.fixture.write_lock(profile=invalid)
                with self.assertRaisesRegex(resolver.ResolutionError, "profile"):
                    resolver.load_lock(self.fixture.lock_path)

    def test_symlinked_interface_is_rejected(self):
        checkout = self.fixture.clone()
        interface = checkout / "specs" / "twin-interface.json"
        content = interface.read_bytes()
        interface.unlink()
        target = checkout / "actual-interface.json"
        target.write_bytes(content)
        interface.symlink_to(target)
        with self.assertRaisesRegex(resolver.ResolutionError, "symlinks"):
            self.resolve(explicit_root=checkout, allow_dirty=True)

    def test_malformed_lock_fails_closed(self):
        self.fixture.lock_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(resolver.ResolutionError, "malformed"):
            resolver.load_lock(self.fixture.lock_path)

    def test_offline_missing_dependency_fails_without_creating_cache(self):
        with self.assertRaisesRegex(resolver.ResolutionError, "--offline"):
            self.resolve()
        self.assertFalse((self.twin_root / ".cache").exists())

    def test_managed_cache_rejects_symlinked_parent_without_touching_target(self):
        for component in (".cache", ".cache/deps", ".cache/deps/firmware"):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory(dir=self.base) as case_raw:
                    case = Path(case_raw)
                    twin = case / "Atom-NeptuneSDR-Twin"
                    twin.mkdir()
                    outside = case / "outside"
                    outside.mkdir()
                    link = twin / component
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(outside, target_is_directory=True)
                    escaped_target = outside
                    if component == ".cache":
                        escaped_target = (
                            escaped_target / "deps" / "firmware" / self.fixture.commit
                        )
                    elif component == ".cache/deps":
                        escaped_target = (
                            escaped_target / "firmware" / self.fixture.commit
                        )
                    else:
                        escaped_target /= self.fixture.commit
                    escaped_target.mkdir(parents=True)
                    marker = escaped_target / "must-survive"
                    marker.write_text("outside\n", encoding="utf-8")

                    with self.assertRaisesRegex(
                        resolver.ResolutionError, "cannot contain symlinks"
                    ):
                        self.resolve(repo_root=twin, offline=False)
                    self.assertEqual(marker.read_text(encoding="utf-8"), "outside\n")

    def test_managed_cache_rejects_symlink_target_without_touching_destination(self):
        managed = self.twin_root / ".cache" / "deps" / "firmware"
        managed.mkdir(parents=True)
        outside = self.base / "outside-target"
        outside.mkdir()
        marker = outside / "must-survive"
        marker.write_text("outside\n", encoding="utf-8")
        (managed / self.fixture.commit).symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            resolver.ResolutionError, "cannot contain symlinks"
        ):
            self.resolve(offline=False)
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside\n")

    def test_skip_worktree_cannot_hide_a_modified_tracked_file(self):
        checkout = self.fixture.clone()
        hidden = checkout / "firmware" / "runtime.txt"
        git(checkout, "update-index", "--skip-worktree", "firmware/runtime.txt")
        hidden.write_text("tampered runtime\n", encoding="utf-8")
        self.assertEqual(git(checkout, "status", "--porcelain=v1"), "")

        with self.assertRaisesRegex(resolver.ResolutionError, "skip-worktree"):
            self.resolve(explicit_root=checkout)
        self.assertEqual(hidden.read_text(encoding="utf-8"), "tampered runtime\n")

    def test_assume_unchanged_cannot_hide_a_modified_tracked_file(self):
        checkout = self.fixture.clone()
        hidden = checkout / "firmware" / "runtime.txt"
        git(checkout, "update-index", "--assume-unchanged", "firmware/runtime.txt")
        hidden.write_text("tampered runtime\n", encoding="utf-8")
        self.assertEqual(git(checkout, "status", "--porcelain=v1"), "")

        with self.assertRaisesRegex(resolver.ResolutionError, "assume-unchanged"):
            self.resolve(explicit_root=checkout)
        self.assertEqual(hidden.read_text(encoding="utf-8"), "tampered runtime\n")

    def test_online_resolution_fetches_only_the_locked_commit_into_cache(self):
        (self.fixture.remote / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.fixture.remote, "add", ".")
        git(self.fixture.remote, "commit", "--quiet", "-m", "later")
        remote_tip = git(self.fixture.remote, "rev-parse", "HEAD")
        self.assertNotEqual(remote_tip, self.fixture.commit)
        result = self.resolve(offline=False)
        expected = (
            self.twin_root / ".cache" / "deps" / "firmware" / self.fixture.commit
        ).resolve()
        self.assertEqual(Path(result.resolved_root), expected)
        self.assertEqual(git(expected, "rev-parse", "HEAD"), self.fixture.commit)
        self.assertFalse((expected / "later.txt").exists())
        offline = self.resolve(offline=True)
        self.assertEqual(offline.resolved_root, result.resolved_root)

    def test_invalid_managed_cache_is_replaced_but_sibling_is_not(self):
        target = (
            self.twin_root / ".cache" / "deps" / "firmware" / self.fixture.commit
        )
        target.mkdir(parents=True)
        (target / "junk").write_text("managed cache\n", encoding="utf-8")
        result = self.resolve(offline=False)
        self.assertEqual(Path(result.resolved_root), target.resolve())
        self.assertFalse((target / "junk").exists())

        sibling = self.twin_root.parent / "Atom-NeptuneSDR-Firmware"
        sibling.mkdir()
        marker = sibling / "do-not-mutate"
        marker.write_text("mine\n", encoding="utf-8")
        with self.assertRaises(resolver.ResolutionError):
            self.resolve(offline=False)
        self.assertEqual(marker.read_text(encoding="utf-8"), "mine\n")

    def test_cli_plain_and_json_outputs_are_machine_readable(self):
        checkout = self.fixture.clone()
        plain = subprocess.run(
            (
                sys.executable,
                str(SCRIPT),
                "--lock",
                str(self.fixture.lock_path),
                "--root",
                str(checkout),
                "--offline",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(plain.stdout.strip(), str(checkout.resolve()))
        detailed = subprocess.run(
            (
                sys.executable,
                str(SCRIPT),
                "--lock",
                str(self.fixture.lock_path),
                "--root",
                str(checkout),
                "--offline",
                "--json",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        payload = json.loads(detailed.stdout)
        self.assertEqual(payload["locked_commit"], self.fixture.commit)
        self.assertTrue(payload["release_ready"])


if __name__ == "__main__":
    unittest.main()
