#!/usr/bin/env python3
"""Validate the locked P210 and official Pluto firmware without flashing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Dict, List, Optional, Tuple
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPOSITORY / "src"
if str(SOURCE_TREE) not in sys.path:
    sys.path.insert(0, str(SOURCE_TREE))

from neptunesdr_twin.boot_harness import (  # noqa: E402
    extract_boot_artifacts,
    fetch_locked_to_cache,
    locked_artifact_path,
    verify_locked_artifact,
)
from neptunesdr_twin.errors import FirmwareFormatError  # noqa: E402
from neptunesdr_twin.firmware import (  # noqa: E402
    DFUSuffix,
    FlattenedDeviceTree,
    load_firmware_lock,
    sha256_bytes,
    validate_fit_image,
    validate_p210_firmware,
)


P210_LOCK_NAME = "p210-sd-boot"
PLUTO_LOCK_NAME = "plutosdr-fw-v0.39"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run parser, integrity, and extraction checks against a P210 boot "
            "bundle and an official Pluto release ZIP or pluto.frm. This is a "
            "host-file test only and contains no flashing path."
        )
    )
    parser.add_argument("--p210", type=Path, help="P210 SD boot tar/directory")
    parser.add_argument("--pluto", type=Path, help="official Pluto ZIP or pluto.frm/pluto.dfu")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPOSITORY / ".cache" / "firmware",
        help="locked artifact cache root",
    )
    parser.add_argument("--lock", type=Path, help="alternate firmware lock JSON")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fetch missing locked artifacts before testing (downloads only; never flashes)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    return parser


def _source_for(
    explicit: Optional[Path],
    name: str,
    cache: Path,
    lock: Optional[Path],
    fetch: bool,
) -> Tuple[Path, bool]:
    if explicit is not None:
        return explicit, False
    path = locked_artifact_path(name, cache, lock)
    if not path.exists() and fetch:
        path = fetch_locked_to_cache(name, cache, lock)
    if not path.exists():
        raise FileNotFoundError(
            "%s is not cached at %s; pass --fetch or an explicit path" % (name, path)
        )
    verify_locked_artifact(name, path, lock)
    return path, True


def _artifact_summary(artifacts: object) -> Dict[str, object]:
    return {
        "kind": artifacts.kind,
        "configuration": artifacts.configuration,
        "hashes": dict(artifacts.hashes),
        "kernel_bytes": artifacts.kernel.stat().st_size,
        "devicetree_bytes": artifacts.devicetree.stat().st_size,
        "ramdisk_bytes": artifacts.ramdisk.stat().st_size if artifacts.ramdisk else None,
        "non_emulated_components": artifacts.non_emulated_components,
    }


def _validate_p210(path: Path) -> Dict[str, object]:
    report = validate_p210_firmware(path)
    result: Dict[str, object] = report.to_dict()
    with tempfile.TemporaryDirectory(prefix="p210-firmware-test-") as temporary:
        artifacts = extract_boot_artifacts(path, Path(temporary))
        result["extraction"] = _artifact_summary(artifacts)
        # Re-parse the exact bytes that QEMU would receive.
        FlattenedDeviceTree(artifacts.devicetree.read_bytes())
    return result


def _fit_result(data: bytes, source: str) -> Dict[str, object]:
    report = validate_fit_image(data, source)
    return report.to_dict()


def _require_pluto_identity(data: bytes, source: str) -> None:
    tree = FlattenedDeviceTree(data)
    if tree.root.string("magic") != "ITB PlutoSDR (ADALM-PLUTO)":
        raise FirmwareFormatError("%s lacks the official Pluto FIT identity marker" % source)


def _validate_pluto(path: Path) -> Dict[str, object]:
    result: Dict[str, object] = {"source": str(path), "compatible": True, "images": {}}
    fit_for_extraction: Optional[bytes] = None
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                candidates = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                ]
                members = {
                    name: [
                        info
                        for info in candidates
                        if PurePosixPath(info.filename).name == name
                    ]
                    for name in ("pluto.frm", "pluto.dfu")
                }
                missing = sorted(name for name, matches in members.items() if not matches)
                if missing:
                    raise FirmwareFormatError(
                        "official Pluto ZIP lacks required member(s): %s" % ", ".join(missing)
                    )
                duplicates = sorted(name for name, matches in members.items() if len(matches) != 1)
                if duplicates:
                    raise FirmwareFormatError(
                        "official Pluto ZIP has ambiguous member(s): %s" % ", ".join(duplicates)
                    )
                for name, matches in members.items():
                    if matches[0].file_size > 256 * 1024 * 1024:
                        raise FirmwareFormatError("%s exceeds the 256 MiB read limit" % name)
                frm = archive.read(members["pluto.frm"][0])
                dfu = archive.read(members["pluto.dfu"][0])
        except zipfile.BadZipFile as exc:
            raise FirmwareFormatError("not a readable Pluto release ZIP") from exc
        _require_pluto_identity(frm, str(path) + "!/pluto.frm")
        _require_pluto_identity(dfu, str(path) + "!/pluto.dfu")
        frm_report = _fit_result(frm, str(path) + "!/pluto.frm")
        dfu_report = _fit_result(dfu, str(path) + "!/pluto.dfu")
        # Parse the DFU suffix independently so VID/PID and CRC are explicit.
        suffix = DFUSuffix.parse(dfu)
        result["images"] = {
            "pluto.frm": frm_report,
            "pluto.dfu": dfu_report,
        }
        result["dfu_identity"] = {
            "vendor_id": suffix.vendor_id,
            "product_id": suffix.product_id,
            "bcd_device": suffix.bcd_device,
            "bcd_dfu": suffix.bcd_dfu,
        }
        result["zip_sha256"] = sha256_bytes(path.read_bytes())
        result["compatible"] = bool(frm_report["compatible"] and dfu_report["compatible"])
        fit_for_extraction = frm
    else:
        if path.stat().st_size > 256 * 1024 * 1024:
            raise FirmwareFormatError("Pluto FIT exceeds the 256 MiB read limit")
        data = path.read_bytes()
        _require_pluto_identity(data, str(path))
        fit_report = _fit_result(data, str(path))
        result["images"] = {path.name: fit_report}
        result["compatible"] = bool(fit_report["compatible"])
        fit_for_extraction = data

    with tempfile.TemporaryDirectory(prefix="pluto-firmware-test-") as temporary:
        # Use the public path detector for a ZIP and direct FIT parser for bytes.
        if zipfile.is_zipfile(path):
            artifacts = extract_boot_artifacts(path, Path(temporary))
        else:
            from neptunesdr_twin.boot_harness import extract_fit_image

            artifacts = extract_fit_image(fit_for_extraction or b"", Path(temporary), source_name=str(path))
        result["extraction"] = _artifact_summary(artifacts)
        FlattenedDeviceTree(artifacts.devicetree.read_bytes())
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Load early so malformed alternate locks fail before any network access.
        load_firmware_lock(args.lock)
        p210, p210_locked = _source_for(
            args.p210, P210_LOCK_NAME, args.cache_dir, args.lock, args.fetch
        )
        pluto, pluto_locked = _source_for(
            args.pluto, PLUTO_LOCK_NAME, args.cache_dir, args.lock, args.fetch
        )
        p210_result = _validate_p210(p210)
        pluto_result = _validate_pluto(pluto)
        report = {
            "p210": p210_result,
            "pluto": pluto_result,
            "locked_inputs": {"p210": p210_locked, "pluto": pluto_locked},
            "compatible": bool(p210_result["compatible"] and pluto_result["compatible"]),
            "flashing_performed": False,
        }
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("P210 firmware: %s" % ("PASS" if p210_result["compatible"] else "FAIL"))
        print("Pluto firmware: %s" % ("PASS" if pluto_result["compatible"] else "FAIL"))
        print("Firmware extraction: PASS")
        print("Flashing performed: no")
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
