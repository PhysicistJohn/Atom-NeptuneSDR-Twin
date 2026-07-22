#!/usr/bin/env python3
"""Resolve the exact Atom-NeptuneSDR Firmwave source pinned by the Twin.

The resolver deliberately treats user-managed checkouts as immutable inputs.
An explicit, environment, or sibling checkout must already match the lock; it
is never fetched, checked out, cleaned, or otherwise modified.  Only the
twin-owned ``.cache/deps`` checkout may be created or replaced.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit


LOCK_SCHEMA_VERSION = 1
DEFAULT_INTERFACE_PATH = "specs/p210-firmware-interface-v1.json"
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCP_REMOTE = re.compile(r"(?:[^@/:]+@)?([^/:]+):(.+)\Z")


class ResolutionError(RuntimeError):
    """The locked Firmwave dependency could not be resolved safely."""


@dataclass(frozen=True)
class FirmwaveLock:
    repository_url: str
    commit: str
    tree: str
    interface_path: str
    interface_sha256: str


@dataclass(frozen=True)
class Resolution:
    resolved_root: str
    source: str
    lock_path: str
    repository_url: str
    canonical_repository: str
    locked_commit: str
    head_commit: str
    locked_tree: str
    head_tree: str
    interface_path: str
    interface_sha256: str
    clean: bool
    release_ready: bool
    non_release_overrides: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["non_release_overrides"] = list(self.non_release_overrides)
        return payload


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ResolutionError(f"{field} must be a JSON object")
    return value


def _require_string(mapping: Mapping[str, Any], key: str, field: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ResolutionError(f"{field}.{key} must be a non-empty string")
    if "\x00" in value:
        raise ResolutionError(f"{field}.{key} contains a NUL byte")
    return value


def _validate_interface_path(raw: str) -> str:
    if "\\" in raw:
        raise ResolutionError("interface.path must use canonical POSIX separators")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw != path.as_posix():
        raise ResolutionError("interface.path must be a canonical relative path")
    if not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ResolutionError("interface.path contains path traversal")
    if path.parts[0] == ".git":
        raise ResolutionError("interface.path cannot address Git metadata")
    return raw


def load_lock(path: Path) -> FirmwaveLock:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResolutionError(f"cannot read Firmwave lock {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ResolutionError(f"malformed Firmwave lock {path}: {exc}") from exc
    root = _require_mapping(document, "lock")
    version = root.get("schema_version")
    if type(version) is not int or version != LOCK_SCHEMA_VERSION:
        raise ResolutionError(
            f"schema_version must be integer {LOCK_SCHEMA_VERSION}, got {version!r}"
        )
    repository = _require_mapping(root.get("repository"), "repository")
    interface = _require_mapping(root.get("interface"), "interface")
    url = _require_string(repository, "url", "repository")
    commit = _require_string(repository, "commit", "repository")
    tree = _require_string(repository, "tree", "repository")
    interface_path = _validate_interface_path(
        _require_string(interface, "path", "interface")
    )
    interface_sha256 = _require_string(interface, "sha256", "interface")
    if not _OBJECT_ID.fullmatch(commit):
        raise ResolutionError("repository.commit must be a full lowercase Git object ID")
    if not _OBJECT_ID.fullmatch(tree) or len(tree) != len(commit):
        raise ResolutionError(
            "repository.tree must be a full lowercase Git object ID of the same width"
        )
    if not _SHA256.fullmatch(interface_sha256):
        raise ResolutionError("interface.sha256 must be a lowercase SHA-256 digest")
    canonical_remote(url)  # Validate before the URL reaches Git.
    return FirmwaveLock(url, commit, tree, interface_path, interface_sha256)


def canonical_remote(remote: str, *, cwd: Optional[Path] = None) -> str:
    """Return a credential-free identity for a Git remote URL."""

    remote = remote.strip()
    if not remote or "\x00" in remote or "\n" in remote or "\r" in remote:
        raise ResolutionError("repository URL is empty or contains control characters")
    scp = _SCP_REMOTE.fullmatch(remote)
    if scp and "://" not in remote:
        host, raw_path = scp.groups()
        path = raw_path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not path or path.startswith("/") or "/../" in f"/{path}/":
            raise ResolutionError("repository URL has a malformed path")
        return f"git://{host.lower()}/{path}"

    parsed = urlsplit(remote)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme == "file":
            if parsed.query or parsed.fragment or parsed.netloc not in ("", "localhost"):
                raise ResolutionError("file repository URL must be local and unadorned")
            local = Path(unquote(parsed.path))
            return "file://" + str(local.resolve())
        if scheme not in ("https", "http", "ssh", "git"):
            raise ResolutionError(f"unsupported repository URL scheme: {parsed.scheme}")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise ResolutionError("repository URL must have a host and no query or fragment")
        path = unquote(parsed.path).strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ResolutionError("repository URL has a malformed path")
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"git://{parsed.hostname.lower()}{port}/{path}"

    local = Path(remote).expanduser()
    if not local.is_absolute():
        local = (cwd or Path.cwd()) / local
    return "file://" + str(local.resolve())


def _git(root: Path, *arguments: str) -> str:
    command = ("git", "-C", str(root), *arguments)
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        raise ResolutionError(f"cannot execute Git: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise ResolutionError(f"{' '.join(arguments)}: {detail}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ResolutionError(f"cannot hash interface {path}: {exc}") from exc
    return digest.hexdigest()


def _interface_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ResolutionError(
            f"interface path escapes the Firmwave checkout or does not exist: {relative}"
        ) from exc
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ResolutionError(f"interface path cannot contain symlinks: {relative}")
    if not resolved.is_file():
        raise ResolutionError(f"interface is not a regular file: {relative}")
    return resolved


def verify_checkout(
    root: Path,
    lock: FirmwaveLock,
    *,
    lock_path: Path,
    source: str,
    allow_dirty: bool = False,
) -> Resolution:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ResolutionError(f"{source} Firmwave root is not a directory: {root}")
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise ResolutionError(f"Firmwave root is not the Git checkout root: {root}")
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head != lock.commit:
        raise ResolutionError(
            f"{source} checkout commit mismatch: expected {lock.commit}, found {head}"
        )
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}")
    if tree != lock.tree:
        raise ResolutionError(
            f"{source} checkout tree mismatch: expected {lock.tree}, found {tree}"
        )
    remote_urls = _git(root, "remote", "get-url", "--all", "origin").splitlines()
    expected_remote = canonical_remote(lock.repository_url, cwd=lock_path.parent)
    actual_remotes = {
        canonical_remote(value, cwd=root) for value in remote_urls if value.strip()
    }
    if expected_remote not in actual_remotes:
        rendered = ", ".join(sorted(actual_remotes)) or "<missing origin>"
        raise ResolutionError(
            f"{source} checkout origin mismatch: expected {expected_remote}, found {rendered}"
        )
    interface = _interface_file(root, lock.interface_path)
    try:
        tracked = _git(root, "ls-files", "--error-unmatch", "--", lock.interface_path)
    except ResolutionError as exc:
        raise ResolutionError(
            f"canonical interface is not tracked by the locked commit: {lock.interface_path}"
        ) from exc
    if tracked != lock.interface_path:
        raise ResolutionError(
            f"canonical interface has an ambiguous Git path: {lock.interface_path}"
        )
    interface_digest = _sha256(interface)
    if interface_digest != lock.interface_sha256:
        raise ResolutionError(
            "canonical interface SHA-256 mismatch: "
            f"expected {lock.interface_sha256}, found {interface_digest}"
        )
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    clean = not status
    if not clean and not allow_dirty:
        raise ResolutionError(
            f"{source} checkout is dirty; refusing a non-reproducible dependency"
        )
    overrides = ("allow-dirty",) if not clean else ()
    return Resolution(
        resolved_root=str(root),
        source=source,
        lock_path=str(lock_path.resolve()),
        repository_url=lock.repository_url,
        canonical_repository=expected_remote,
        locked_commit=lock.commit,
        head_commit=head,
        locked_tree=lock.tree,
        head_tree=tree,
        interface_path=lock.interface_path,
        interface_sha256=interface_digest,
        clean=clean,
        release_ready=clean,
        non_release_overrides=overrides,
    )


def _existing(path: Path) -> bool:
    return os.path.lexists(str(path.expanduser()))


def _populate_cache(
    target: Path, lock: FirmwaveLock, lock_path: Path
) -> Resolution:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{lock.commit}.", dir=str(parent)))
    try:
        _git(temporary, "init", "--quiet")
        _git(temporary, "remote", "add", "origin", lock.repository_url)
        _git(temporary, "fetch", "--quiet", "--depth", "1", "origin", lock.commit)
        _git(temporary, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        result = verify_checkout(
            temporary,
            lock,
            lock_path=lock_path,
            source="managed-cache",
        )
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ResolutionError(f"managed cache target is not a safe directory: {target}")
        if target.exists():
            shutil.rmtree(target)
        os.replace(str(temporary), str(target))
        return verify_checkout(
            target,
            lock,
            lock_path=lock_path,
            source="managed-cache",
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def resolve_firmwave(
    *,
    repo_root: Path,
    lock_path: Path,
    explicit_root: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    offline: bool = False,
    allow_dirty: bool = False,
) -> Resolution:
    repo_root = repo_root.expanduser().resolve()
    lock_path = lock_path.expanduser().resolve()
    lock = load_lock(lock_path)
    environment = os.environ if environment is None else environment

    if explicit_root is not None:
        return verify_checkout(
            explicit_root,
            lock,
            lock_path=lock_path,
            source="explicit",
            allow_dirty=allow_dirty,
        )
    env_value = environment.get("NEPTUNESDR_FIRMWAVE_ROOT", "").strip()
    if env_value:
        return verify_checkout(
            Path(env_value),
            lock,
            lock_path=lock_path,
            source="environment",
            allow_dirty=allow_dirty,
        )
    sibling = repo_root.parent / "Atom-NeptuneSDR_Firmwave"
    if _existing(sibling):
        return verify_checkout(
            sibling,
            lock,
            lock_path=lock_path,
            source="sibling",
            allow_dirty=allow_dirty,
        )

    target = repo_root / ".cache" / "deps" / "firmwave" / lock.commit
    if _existing(target):
        try:
            return verify_checkout(
                target,
                lock,
                lock_path=lock_path,
                source="managed-cache",
                allow_dirty=allow_dirty,
            )
        except ResolutionError:
            if offline:
                raise
    elif offline:
        raise ResolutionError(
            "Firmwave dependency is not available locally and --offline forbids fetching"
        )
    return _populate_cache(target, lock, lock_path)


def _parser(repo_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=repo_root / "deps" / "firmwave.lock.json",
        help="dependency lock (default: deps/firmwave.lock.json)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="validate this checkout; never mutates it",
    )
    parser.add_argument(
        "--offline", action="store_true", help="forbid network access and cloning"
    )
    parser.add_argument("--json", action="store_true", help="emit full resolution JSON")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="NON-RELEASE ONLY: accept a dirty checkout and mark it non-release",
    )
    return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = _parser(repo_root)
    args = parser.parse_args(arguments)
    try:
        result = resolve_firmwave(
            repo_root=repo_root,
            lock_path=args.lock,
            explicit_root=args.root,
            offline=args.offline,
            allow_dirty=args.allow_dirty,
        )
    except ResolutionError as exc:
        print(f"firmwave resolution failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        print(result.resolved_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
