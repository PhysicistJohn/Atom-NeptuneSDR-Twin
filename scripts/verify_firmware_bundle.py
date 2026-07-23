#!/usr/bin/env python3
"""Independently verify the Firmware runtime bundle consumed by the Twin."""

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


RUNTIME_SCHEMA = "neptunesdr.firmware.runtime-manifest/v1"
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
    firmware_root: Path,
    runtime: Path,
    lock_path: Path,
) -> Dict[str, Any]:
    firmware_root = firmware_root.resolve(strict=True)
    runtime = runtime.resolve(strict=True)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = _load_object(manifest_path, "runtime manifest")
    lock = _load_object(lock_path, "Firmware dependency lock")

    if manifest.get("schema") != RUNTIME_SCHEMA:
        raise ValueError("unsupported Firmware runtime manifest schema")
    if manifest.get("profile") != "qemu-development":
        raise ValueError("Firmware bundle is not the qemu-development profile")
    if manifest.get("flashable") is not False:
        raise ValueError("Firmware bundle must explicitly be non-flashable")
    if manifest.get("artifact_hashes_complete") is not True:
        raise ValueError("Firmware manifest does not declare complete artifact hashes")

    source = manifest.get("firmware_source")
    if not isinstance(source, dict):
        raise ValueError("Firmware manifest has no source identity")
    current = _source_state(firmware_root)
    current_tree = subprocess.check_output(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=str(firmware_root),
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()
    expected_source = {
        "repository": "Atom-NeptuneSDR-Firmware",
        "commit": current["commit"],
        "state_sha256": current["state_sha256"],
        "clean": True,
        "tree": current_tree,
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            raise ValueError("Firmware source %s does not match the resolved checkout" % key)
    if not current["clean"]:
        raise ValueError("Firmware checkout is dirty")

    repository = lock.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("Firmware dependency lock has no repository object")
    if repository.get("commit") != current["commit"]:
        raise ValueError("Firmware source commit does not match the dependency lock")
    if repository.get("tree") != current_tree:
        raise ValueError("Firmware source tree does not match the dependency lock")

    interface = manifest.get("interface")
    locked_interface = lock.get("interface")
    if not isinstance(interface, dict) or not isinstance(locked_interface, dict):
        raise ValueError("Firmware interface identity is incomplete")
    if interface.get("schema") != INTERFACE_SCHEMA:
        raise ValueError("unsupported Firmware interface schema")
    interface_path = _safe_relative(interface.get("path"), "Firmware interface path")
    if str(interface_path) != locked_interface.get("path"):
        raise ValueError("Firmware interface path does not match the dependency lock")
    actual_interface = (firmware_root / Path(*interface_path.parts)).resolve(strict=True)
    if firmware_root not in actual_interface.parents:
        raise ValueError("Firmware interface path escapes its repository")
    interface_sha = _sha256(actual_interface)
    if interface.get("sha256") != interface_sha:
        raise ValueError("Firmware manifest interface hash is invalid")
    if locked_interface.get("sha256") != interface_sha:
        raise ValueError("Firmware interface hash does not match the dependency lock")
    interface_document = _load_object(actual_interface, "Firmware interface")
    if interface_document.get("schema") != INTERFACE_SCHEMA:
        raise ValueError("locked Firmware interface has an unsupported schema")
    produced = interface_document.get("produced_artifacts")
    if not isinstance(produced, dict):
        raise ValueError("Firmware interface has no produced_artifacts mapping")
    canonical_paths: Dict[str, PurePosixPath] = {}
    for name in REQUIRED_ARTIFACTS:
        canonical_paths[name] = _safe_relative(
            produced.get(name), "Firmware interface artifact %s path" % name
        )

    generated = manifest.get("generated_artifacts")
    if not isinstance(generated, dict):
        raise ValueError("Firmware manifest has no generated artifact mapping")
    missing = [name for name in REQUIRED_ARTIFACTS if name not in generated]
    if missing:
        raise ValueError("Firmware bundle lacks required artifacts: %s" % ", ".join(missing))
    for name, canonical in canonical_paths.items():
        raw = generated[name]
        if not isinstance(raw, Mapping):
            raise ValueError("Firmware generated artifact entries must be named objects")
        declared = _safe_relative(
            raw.get("path"), "generated artifact %s path" % name
        )
        if declared != canonical or raw.get("path") != produced.get(name):
            raise ValueError(
                "Firmware generated artifact %s path does not match the locked interface"
                % name
            )

    verified: Dict[str, Dict[str, Any]] = {}
    seen_paths = set()
    for name, raw in sorted(generated.items()):
        if not isinstance(name, str) or not name or not isinstance(raw, Mapping):
            raise ValueError("Firmware generated artifact entries must be named objects")
        relative = _safe_relative(raw.get("path"), "generated artifact %s path" % name)
        if relative in seen_paths:
            raise ValueError("Firmware generated artifacts reuse path %s" % relative)
        seen_paths.add(relative)
        path = (runtime / Path(*relative.parts)).resolve(strict=True)
        if runtime not in path.parents:
            raise ValueError("Firmware generated artifact escapes the runtime directory")
        if not path.is_file():
            raise ValueError("Firmware generated artifact is not a regular file: %s" % path)
        size = path.stat().st_size
        digest = _sha256(path)
        if raw.get("bytes") != size or raw.get("sha256") != digest:
            raise ValueError("Firmware generated artifact hash/size mismatch: %s" % name)
        role = raw.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("Firmware generated artifact has no role: %s" % name)
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
        "firmware_source": source,
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
    parser.add_argument("--firmware-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "deps" / "firmware.lock.json")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_bundle(args.firmware_root, args.runtime, args.lock)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print("verify_firmware_bundle.py: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("FIRMWARE_BUNDLE VERIFIED")
        print("manifest_sha256=%s" % result["manifest_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
