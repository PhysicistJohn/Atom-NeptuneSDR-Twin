#!/usr/bin/env python3
"""Independently verify the Firmwave runtime bundle consumed by the Twin."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any, Dict, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from acceptance_gate import _source_state  # noqa: E402


RUNTIME_SCHEMA = "neptunesdr.firmwave.runtime-manifest/v1"
INTERFACE_SCHEMA = "neptunesdr.p210-firmware-interface/v1"
REQUIRED_ARTIFACTS = ("kernel", "devicetree", "rootfs", "fft_runtime")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("%s must contain a JSON object" % label)
    return value


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty relative path" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError("%s is not a safe relative path" % label)
    return path


def verify_bundle(
    firmwave_root: Path,
    runtime: Path,
    lock_path: Path,
) -> Dict[str, Any]:
    firmwave_root = firmwave_root.resolve(strict=True)
    runtime = runtime.resolve(strict=True)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = _load_object(manifest_path, "runtime manifest")
    lock = _load_object(lock_path, "Firmwave dependency lock")

    if manifest.get("schema") != RUNTIME_SCHEMA:
        raise ValueError("unsupported Firmwave runtime manifest schema")
    if manifest.get("profile") != "qemu-development":
        raise ValueError("Firmwave bundle is not the qemu-development profile")
    if manifest.get("flashable") is not False:
        raise ValueError("Firmwave bundle must explicitly be non-flashable")
    if manifest.get("artifact_hashes_complete") is not True:
        raise ValueError("Firmwave manifest does not declare complete artifact hashes")

    source = manifest.get("firmwave_source")
    if not isinstance(source, dict):
        raise ValueError("Firmwave manifest has no source identity")
    current = _source_state(firmwave_root)
    current_tree = subprocess.check_output(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=str(firmwave_root),
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()
    expected_source = {
        "repository": "Atom-NeptuneSDR_Firmwave",
        "commit": current["commit"],
        "state_sha256": current["state_sha256"],
        "clean": True,
        "tree": current_tree,
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            raise ValueError("Firmwave source %s does not match the resolved checkout" % key)
    if not current["clean"]:
        raise ValueError("Firmwave checkout is dirty")

    repository = lock.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("Firmwave dependency lock has no repository object")
    if repository.get("commit") != current["commit"]:
        raise ValueError("Firmwave source commit does not match the dependency lock")
    if repository.get("tree") != current_tree:
        raise ValueError("Firmwave source tree does not match the dependency lock")

    interface = manifest.get("interface")
    locked_interface = lock.get("interface")
    if not isinstance(interface, dict) or not isinstance(locked_interface, dict):
        raise ValueError("Firmwave interface identity is incomplete")
    if interface.get("schema") != INTERFACE_SCHEMA:
        raise ValueError("unsupported Firmwave interface schema")
    interface_path = _safe_relative(interface.get("path"), "Firmwave interface path")
    if str(interface_path) != locked_interface.get("path"):
        raise ValueError("Firmwave interface path does not match the dependency lock")
    actual_interface = (firmwave_root / Path(*interface_path.parts)).resolve(strict=True)
    if firmwave_root not in actual_interface.parents:
        raise ValueError("Firmwave interface path escapes its repository")
    interface_sha = _sha256(actual_interface)
    if interface.get("sha256") != interface_sha:
        raise ValueError("Firmwave manifest interface hash is invalid")
    if locked_interface.get("sha256") != interface_sha:
        raise ValueError("Firmwave interface hash does not match the dependency lock")

    generated = manifest.get("generated_artifacts")
    if not isinstance(generated, dict):
        raise ValueError("Firmwave manifest has no generated artifact mapping")
    missing = [name for name in REQUIRED_ARTIFACTS if name not in generated]
    if missing:
        raise ValueError("Firmwave bundle lacks required artifacts: %s" % ", ".join(missing))

    verified: Dict[str, Dict[str, Any]] = {}
    seen_paths = set()
    for name, raw in sorted(generated.items()):
        if not isinstance(name, str) or not name or not isinstance(raw, Mapping):
            raise ValueError("Firmwave generated artifact entries must be named objects")
        relative = _safe_relative(raw.get("path"), "generated artifact %s path" % name)
        if relative in seen_paths:
            raise ValueError("Firmwave generated artifacts reuse path %s" % relative)
        seen_paths.add(relative)
        path = (runtime / Path(*relative.parts)).resolve(strict=True)
        if runtime not in path.parents:
            raise ValueError("Firmwave generated artifact escapes the runtime directory")
        if not path.is_file():
            raise ValueError("Firmwave generated artifact is not a regular file: %s" % path)
        size = path.stat().st_size
        digest = _sha256(path)
        if raw.get("bytes") != size or raw.get("sha256") != digest:
            raise ValueError("Firmwave generated artifact hash/size mismatch: %s" % name)
        role = raw.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("Firmwave generated artifact has no role: %s" % name)
        verified[name] = {
            "path": str(relative),
            "bytes": size,
            "sha256": digest,
            "role": role,
        }

    return {
        "status": "verified",
        "profile": "qemu-development",
        "flashable": False,
        "firmwave_source": source,
        "interface": {
            "path": str(interface_path),
            "sha256": interface_sha,
            "schema": INTERFACE_SCHEMA,
        },
        "artifacts": verified,
        "manifest_sha256": _sha256(manifest_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmwave-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "deps" / "firmwave.lock.json")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_bundle(args.firmwave_root, args.runtime, args.lock)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print("verify_firmwave_bundle.py: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("FIRMWAVE_BUNDLE VERIFIED")
        print("manifest_sha256=%s" % result["manifest_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
