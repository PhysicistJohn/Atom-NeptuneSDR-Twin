#!/usr/bin/env python3
"""Fetch locked firmware into a content-addressed cache; never flash it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from neptunesdr_twin.boot_harness import fetch_locked_to_cache  # noqa: E402
from neptunesdr_twin.firmware import load_firmware_lock  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download firmware named in firmware-lock.json into "
            "CACHE/sha256/DIGEST. This command only writes ordinary host files; "
            "it never discovers or flashes a USB device."
        )
    )
    parser.add_argument(
        "artifacts",
        nargs="*",
        metavar="NAME",
        help="locked artifact name (default: every artifact in the lock)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPOSITORY / ".cache" / "firmware",
        help="content-addressed cache root",
    )
    parser.add_argument("--lock", type=Path, help="alternate firmware lock JSON")
    parser.add_argument("--force", action="store_true", help="re-download even if a valid file exists")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    lock = load_firmware_lock(args.lock)
    entries = lock.get("artifacts", {})
    if not isinstance(entries, dict):
        print("error: firmware lock has no artifact mapping", file=sys.stderr)
        return 2
    names = args.artifacts or sorted(entries)
    unknown = [name for name in names if name not in entries]
    if unknown:
        print("error: unknown locked artifact(s): %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    results = []
    try:
        for name in names:
            path = fetch_locked_to_cache(name, args.cache_dir, args.lock, force=args.force)
            entry = entries[name]
            results.append(
                {
                    "name": name,
                    "path": str(path.resolve()),
                    "sha256": entry["sha256"],
                    "bytes": path.stat().st_size,
                }
            )
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for result in results:
            print("{name}\t{sha256}\t{bytes}\t{path}".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
