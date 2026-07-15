"""Read-only firmware extraction and bounded QEMU boot smoke tests.

The harness deliberately implements direct-kernel boot only.  It never opens a
block device, exposes a host USB device, or invokes a firmware flashing tool.
Firmware containers are parsed in-process and QEMU is executed without a
shell.  The command builder also disables networking and the QEMU monitor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import Dict, Iterable, List, Mapping, Optional, Pattern, Sequence, Tuple, Union
from urllib.parse import unquote, urlparse
import zipfile

from .errors import FirmwareFormatError
from .firmware import (
    FDT_MAGIC,
    FDTNode,
    FirmwareBundle,
    FlattenedDeviceTree,
    UImage,
    fetch_locked_artifact,
    load_firmware_lock,
    sha256_bytes,
    validate_fit_image,
    validate_p210_firmware,
)


DEFAULT_BOOTARGS = (
    "console=ttyPS0,115200 "
    "earlycon=cdns,mmio,0xe0001000,115200n8 "
    "ignore_loglevel"
)
DEFAULT_LOG_PATTERNS = (r"Booting Linux", r"Linux version")
DEFAULT_FATAL_LOG_PATTERNS = (
    r"Kernel panic",
    r"not syncing",
    r"Unable to mount root fs",
)
MAX_BOOT_TIMEOUT_SECONDS = 600.0
MAX_QEMU_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FIT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class BootArtifacts:
    """Host paths and provenance for one direct-kernel boot attempt."""

    source: Path
    kind: str
    kernel: Path
    devicetree: Path
    ramdisk: Optional[Path] = None
    bootargs: str = DEFAULT_BOOTARGS
    configuration: Optional[str] = None
    hashes: Mapping[str, str] = field(default_factory=dict)
    non_emulated_components: Tuple[str, ...] = ()

    @property
    def dtb(self) -> Path:
        """Compatibility alias for callers that use QEMU's option name."""

        return self.devicetree

    @property
    def execution_scope(self) -> str:
        """The strongest runtime claim the extracted inputs can support."""

        if self.ramdisk is None:
            return "kernel-entry-only"
        return "kernel-and-initramfs-entry"


@dataclass(frozen=True)
class QEMUBootResult:
    """Outcome of a bounded QEMU process, including expected log matches."""

    command: Tuple[str, ...]
    output: str
    returncode: Optional[int]
    timed_out: bool
    elapsed_seconds: float
    expected_patterns: Tuple[str, ...]
    matched_patterns: Tuple[str, ...]
    rejected_patterns: Tuple[str, ...] = ()
    matched_rejections: Tuple[str, ...] = ()
    output_truncated: bool = False

    @property
    def missing_patterns(self) -> Tuple[str, ...]:
        matched = set(self.matched_patterns)
        return tuple(pattern for pattern in self.expected_patterns if pattern not in matched)

    @property
    def passed(self) -> bool:
        """The bounded smoke check passes once every requested contact is seen.

        Firmware normally keeps running, so reaching the timeout after all
        contacts have appeared is expected.  This property does not promote
        kernel-entry contacts into a userspace, FPGA, RF, or USB claim.
        """

        if self.expected_patterns:
            return (
                not self.missing_patterns
                and not self.matched_rejections
                and not self.output_truncated
                and (self.timed_out or self.returncode == 0)
            )
        return (
            not self.matched_rejections
            and not self.output_truncated
            and not self.timed_out
            and self.returncode == 0
        )


def locate_qemu_system_arm(
    explicit: Optional[Union[str, os.PathLike[str]]] = None,
    *,
    required: bool = True,
) -> Optional[Path]:
    """Locate an executable ``qemu-system-arm`` without executing it.

    Resolution order is an explicit argument, ``QEMU_SYSTEM_ARM``, and then
    ``PATH``.  An explicit bare command is also resolved through ``PATH``.
    """

    candidate: Optional[str]
    if explicit is not None:
        candidate = os.fspath(explicit)
    else:
        candidate = os.environ.get("QEMU_SYSTEM_ARM")

    resolved: Optional[str] = None
    if candidate:
        candidate_path = Path(candidate).expanduser()
        if candidate_path.parent != Path(".") or candidate_path.is_absolute():
            resolved = os.fspath(candidate_path)
        else:
            resolved = shutil.which(candidate)
    else:
        resolved = shutil.which("qemu-system-arm")

    if resolved:
        path = Path(resolved).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path

    if required:
        detail = " (%s)" % candidate if candidate else ""
        raise FileNotFoundError(
            "qemu-system-arm%s was not found or is not executable; install QEMU, "
            "set QEMU_SYSTEM_ARM, or pass an explicit executable" % detail
        )
    return None


def _atomic_write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _node_reference(configuration: FDTNode, property_name: str) -> Optional[str]:
    # Kept as a helper so all FIT references get identical validation.
    value = configuration.string(property_name)
    if value is None:
        return None
    if not value or "/" in value or "\\" in value or value in (".", ".."):
        raise FirmwareFormatError("unsafe FIT %s image reference %r" % (property_name, value))
    return value


def _find_child(node: FDTNode, name: str) -> FDTNode:
    child = node.child(name)
    if child is None:
        raise FirmwareFormatError("FIT references missing /images/%s" % name)
    return child


def _inline_image_payload(image: FDTNode, expected_type: str) -> Tuple[bytes, str]:
    image_type = image.string("type", "")
    if image_type != expected_type:
        raise FirmwareFormatError(
            "FIT image %s has type %r, expected %r"
            % (image.name, image_type, expected_type)
        )
    payload = image.properties.get("data")
    if payload is None:
        raise FirmwareFormatError("FIT image %s is not inline" % image.name)
    compression = image.string("compression", "none") or "none"
    return payload, compression


def _chosen_bootargs(tree: FlattenedDeviceTree) -> Optional[str]:
    chosen = tree.find("/chosen")
    if chosen is None:
        return None
    return chosen.string("bootargs")


def extract_fit_image(
    source: Union[bytes, bytearray, memoryview, str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
    configuration: Optional[str] = None,
    *,
    source_name: Optional[str] = None,
) -> BootArtifacts:
    """Extract the kernel, ramdisk, and DT selected by an inline-data FIT.

    A ``.frm`` MD5 trailer or a ``.dfu`` suffix is harmless: the existing
    :class:`FlattenedDeviceTree` parser bounds parsing at the FIT total size.
    External-data FIT images are intentionally rejected because following
    arbitrary offsets or paths would weaken the extraction boundary.
    """

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        source_path = Path(source_name or "FIT image")
    else:
        source_path = Path(source)
        if source_path.stat().st_size > MAX_FIT_BYTES:
            raise FirmwareFormatError("FIT image exceeds the 256 MiB read limit")
        data = source_path.read_bytes()

    integrity = validate_fit_image(data, source_name or os.fspath(source_path))
    if not integrity.compatible:
        failures = "; ".join(
            issue.message for issue in integrity.issues if issue.severity == "error"
        )
        raise FirmwareFormatError("FIT integrity validation failed: " + failures)
    tree = FlattenedDeviceTree(data)
    images = tree.find("/images")
    configurations = tree.find("/configurations")
    if images is None or configurations is None:
        raise FirmwareFormatError("FIT lacks /images or /configurations")

    selected_name = configuration or configurations.string("default")
    if selected_name is None:
        if not configurations.children:
            raise FirmwareFormatError("FIT contains no configurations")
        selected_name = configurations.children[0].name
    if "/" in selected_name or "\\" in selected_name or selected_name in (".", ".."):
        raise FirmwareFormatError("unsafe FIT configuration name %r" % selected_name)
    selected = configurations.child(selected_name)
    if selected is None:
        available = ", ".join(child.name for child in configurations.children)
        raise FirmwareFormatError(
            "FIT configuration %r does not exist (available: %s)" % (selected_name, available)
        )

    kernel_reference = _node_reference(selected, "kernel")
    fdt_reference = _node_reference(selected, "fdt")
    ramdisk_reference = _node_reference(selected, "ramdisk")
    fpga_reference = _node_reference(selected, "fpga")
    if kernel_reference is None or fdt_reference is None:
        raise FirmwareFormatError("FIT configuration must reference a kernel and fdt")
    if fpga_reference is not None:
        _find_child(images, fpga_reference)

    kernel_image = _find_child(images, kernel_reference)
    fdt_image = _find_child(images, fdt_reference)
    kernel, kernel_compression = _inline_image_payload(kernel_image, "kernel")
    devicetree, fdt_compression = _inline_image_payload(fdt_image, "flat_dt")
    if kernel_compression != "none":
        raise FirmwareFormatError("compressed FIT kernels are not supported for direct QEMU boot")
    if fdt_compression != "none":
        raise FirmwareFormatError("compressed FIT device trees are not supported")
    devicetree_tree = FlattenedDeviceTree(devicetree)

    ramdisk: Optional[bytes] = None
    ramdisk_compression: Optional[str] = None
    if ramdisk_reference is not None:
        ramdisk_image = _find_child(images, ramdisk_reference)
        ramdisk, ramdisk_compression = _inline_image_payload(ramdisk_image, "ramdisk")
        if ramdisk_compression not in ("none", "gzip"):
            raise FirmwareFormatError(
                "unsupported FIT ramdisk compression %r" % ramdisk_compression
            )
        if ramdisk_compression == "gzip" and not ramdisk.startswith(b"\x1f\x8b"):
            raise FirmwareFormatError("FIT labels the ramdisk as gzip but its magic is absent")

    output = Path(destination)
    kernel_path = _atomic_write(output / "kernel.bin", kernel)
    dtb_path = _atomic_write(output / "devicetree.dtb", devicetree)
    ramdisk_path: Optional[Path] = None
    if ramdisk is not None:
        suffix = ".img.gz" if ramdisk_compression == "gzip" else ".img"
        ramdisk_path = _atomic_write(output / ("ramdisk" + suffix), ramdisk)

    hashes: Dict[str, str] = {
        "kernel": sha256_bytes(kernel),
        "devicetree": sha256_bytes(devicetree),
    }
    if ramdisk is not None:
        hashes["ramdisk"] = sha256_bytes(ramdisk)
    return BootArtifacts(
        source=source_path,
        kind="fit",
        kernel=kernel_path,
        devicetree=dtb_path,
        ramdisk=ramdisk_path,
        bootargs=_chosen_bootargs(devicetree_tree) or DEFAULT_BOOTARGS,
        configuration=selected_name,
        hashes=hashes,
        non_emulated_components=(
            ("FIT /images/" + fpga_reference,) if fpga_reference is not None else ()
        ),
    )


def _bootargs_from_uenv(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "bootargs":
            return value.strip() or None
    return None


def extract_p210_bundle(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
) -> BootArtifacts:
    """Extract a CRC-checked P210 uImage payload and validated device tree."""

    source_path = Path(source)
    report = validate_p210_firmware(source_path)
    if not report.compatible:
        failures = "; ".join(
            issue.message for issue in report.issues if issue.severity == "error"
        )
        raise FirmwareFormatError("P210 bundle validation failed: " + failures)
    bundle = FirmwareBundle(source_path)
    try:
        uimage_data = bundle.files["uImage"]
        devicetree = bundle.files["devicetree.dtb"]
    except KeyError as exc:
        raise FirmwareFormatError("P210 bundle lacks %s" % exc.args[0]) from exc

    image = UImage(uimage_data)
    if image.header.os != 5 or image.header.architecture != 2 or image.header.image_type != 2:
        raise FirmwareFormatError("P210 uImage is not a Linux/ARM kernel image")
    if image.header.compression != 0:
        raise FirmwareFormatError("compressed P210 uImage payloads are not supported")
    tree = FlattenedDeviceTree(devicetree)

    output = Path(destination)
    kernel_path = _atomic_write(output / "kernel.bin", image.payload)
    dtb_path = _atomic_write(output / "devicetree.dtb", devicetree)
    bootargs = _bootargs_from_uenv(bundle.files.get("uEnv.txt"))
    if bootargs is None:
        bootargs = _chosen_bootargs(tree) or DEFAULT_BOOTARGS
    return BootArtifacts(
        source=source_path,
        kind="p210-sd-boot",
        kernel=kernel_path,
        devicetree=dtb_path,
        bootargs=bootargs,
        hashes={
            "kernel": sha256_bytes(image.payload),
            "devicetree": sha256_bytes(devicetree),
        },
        non_emulated_components=("BOOT.BIN (FSBL/U-Boot/FPGA payload)",),
    )


def _pluto_fit_from_zip(source: Path) -> Tuple[str, bytes]:
    try:
        with zipfile.ZipFile(source) as archive:
            matches = [
                info
                for info in archive.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).name == "pluto.frm"
            ]
            if len(matches) != 1:
                raise FirmwareFormatError(
                    "official Pluto archive must contain exactly one pluto.frm"
                )
            info = matches[0]
            # A generous cap prevents a crafted ZIP from expanding without bound.
            if info.file_size > MAX_FIT_BYTES:
                raise FirmwareFormatError("pluto.frm exceeds the 256 MiB extraction limit")
            return info.filename, archive.read(info)
    except zipfile.BadZipFile as exc:
        raise FirmwareFormatError("not a readable Pluto release ZIP") from exc


def extract_pluto_archive(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
    configuration: Optional[str] = None,
) -> BootArtifacts:
    """Extract the configured inline boot images from an official Pluto ZIP."""

    source_path = Path(source)
    member, data = _pluto_fit_from_zip(source_path)
    tree = FlattenedDeviceTree(data)
    if tree.root.string("magic") != "ITB PlutoSDR (ADALM-PLUTO)":
        raise FirmwareFormatError("pluto.frm lacks the official Pluto FIT identity marker")
    result = extract_fit_image(
        data,
        destination,
        configuration,
        source_name="%s!/%s" % (source_path, member),
    )
    return BootArtifacts(
        source=source_path,
        kind="pluto-release-zip",
        kernel=result.kernel,
        devicetree=result.devicetree,
        ramdisk=result.ramdisk,
        bootargs=result.bootargs,
        configuration=result.configuration,
        hashes=result.hashes,
        non_emulated_components=result.non_emulated_components,
    )


def extract_boot_artifacts(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
    configuration: Optional[str] = None,
) -> BootArtifacts:
    """Detect a P210 bundle, Pluto release ZIP, or direct FIT and extract it."""

    source_path = Path(source)
    if zipfile.is_zipfile(source_path):
        return extract_pluto_archive(source_path, destination, configuration)
    if source_path.is_dir() or tarfile.is_tarfile(source_path):
        if configuration is not None:
            raise FirmwareFormatError("P210 tar bundles do not contain FIT configurations")
        return extract_p210_bundle(source_path, destination)
    with source_path.open("rb") as handle:
        magic = handle.read(4)
    if magic == FDT_MAGIC.to_bytes(4, "big"):
        return extract_fit_image(source_path, destination, configuration)
    raise FirmwareFormatError("firmware source is not a P210 bundle, Pluto ZIP, or FIT image")


def build_qemu_command(
    artifacts: BootArtifacts,
    qemu: Union[str, os.PathLike[str]] = "qemu-system-arm",
    *,
    memory_mib: int = 512,
    cpus: int = 2,
    append: Optional[str] = None,
) -> List[str]:
    """Construct a no-network, no-monitor Zynq direct-boot command.

    Paths are separate argv entries and callers must pass the returned sequence
    to ``subprocess`` with ``shell=False`` (as :func:`run_qemu_boot` does).
    """

    if (
        isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or not 16 <= memory_mib <= 2048
    ):
        raise ValueError("memory_mib must be between 16 and 2048 for xilinx-zynq-a9")
    if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= 2:
        raise ValueError("xilinx-zynq-a9 supports one or two Cortex-A9 CPUs")
    kernel = Path(artifacts.kernel).resolve()
    devicetree = Path(artifacts.devicetree).resolve()
    for label, path in (("kernel", kernel), ("device tree", devicetree)):
        if not path.is_file():
            raise FileNotFoundError("%s does not exist: %s" % (label, path))
    ramdisk = Path(artifacts.ramdisk).resolve() if artifacts.ramdisk is not None else None
    if ramdisk is not None and not ramdisk.is_file():
        raise FileNotFoundError("ramdisk does not exist: %s" % ramdisk)

    bootargs = artifacts.bootargs if append is None else append
    if "\0" in bootargs or "\n" in bootargs or "\r" in bootargs:
        raise ValueError("kernel command line contains a control character")
    if len(bootargs.encode("utf-8")) > 4096:
        raise ValueError("kernel command line exceeds 4096 bytes")

    command = [
        os.fspath(qemu),
        "-machine",
        "xilinx-zynq-a9",
        "-cpu",
        "cortex-a9",
        "-smp",
        str(cpus),
        "-m",
        "%dM" % memory_mib,
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "null",
        "-serial",
        "stdio",
        "-nic",
        "none",
        "-no-reboot",
        "-kernel",
        os.fspath(kernel),
        "-dtb",
        os.fspath(devicetree),
    ]
    if ramdisk is not None:
        command.extend(("-initrd", os.fspath(ramdisk)))
    if bootargs:
        command.extend(("-append", bootargs))
    return command


def _compile_patterns(patterns: Iterable[Union[str, Pattern[str]]]) -> Tuple[Tuple[str, ...], Tuple[Pattern[str], ...]]:
    labels: List[str] = []
    compiled: List[Pattern[str]] = []
    for pattern in patterns:
        if isinstance(pattern, str):
            label = pattern
            try:
                regex = re.compile(pattern, re.MULTILINE)
            except re.error as exc:
                raise ValueError("invalid expected log pattern %r: %s" % (pattern, exc)) from exc
        else:
            label = pattern.pattern
            regex = pattern
        labels.append(label)
        compiled.append(regex)
    return tuple(labels), tuple(compiled)


def run_qemu_boot(
    command: Sequence[Union[str, os.PathLike[str]]],
    *,
    timeout: float = 30.0,
    patterns: Iterable[Union[str, Pattern[str]]] = DEFAULT_LOG_PATTERNS,
    reject_patterns: Iterable[Union[str, Pattern[str]]] = DEFAULT_FATAL_LOG_PATTERNS,
    max_output_bytes: int = MAX_QEMU_OUTPUT_BYTES,
) -> QEMUBootResult:
    """Run a QEMU argv sequence, capture its log, and enforce a hard timeout."""

    if not command:
        raise ValueError("QEMU command may not be empty")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= MAX_BOOT_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "timeout must be greater than zero and no more than %.0f seconds"
            % MAX_BOOT_TIMEOUT_SECONDS
        )
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1024 <= max_output_bytes <= 64 * 1024 * 1024
    ):
        raise ValueError("max_output_bytes must be between 1024 and 67108864")
    argv = tuple(os.fspath(part) for part in command)
    expected, compiled = _compile_patterns(patterns)
    rejected, compiled_rejections = _compile_patterns(reject_patterns)
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=(os.name == "posix"),
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        raise RuntimeError("QEMU stdout pipe was not created")
    chunks: List[bytes] = []
    captured_bytes = 0
    output_truncated = False

    def drain_output() -> None:
        nonlocal captured_bytes, output_truncated
        while True:
            try:
                chunk = process.stdout.read(64 * 1024)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            remaining = max_output_bytes - captured_bytes
            if remaining > 0:
                kept = chunk[:remaining]
                chunks.append(kept)
                captured_bytes += len(kept)
            if len(chunk) > remaining:
                output_truncated = True

    reader = threading.Thread(target=drain_output, name="qemu-log-reader", daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
    reader.join(timeout=2.0)
    process.stdout.close()
    if reader.is_alive():
        reader.join(timeout=1.0)
    elapsed = time.monotonic() - started
    output = b"".join(chunks).decode("utf-8", errors="replace")
    matched = tuple(label for label, regex in zip(expected, compiled) if regex.search(output))
    matched_rejections = tuple(
        label
        for label, regex in zip(rejected, compiled_rejections)
        if regex.search(output)
    )
    return QEMUBootResult(
        command=argv,
        output=output,
        returncode=process.returncode,
        timed_out=timed_out,
        elapsed_seconds=elapsed,
        expected_patterns=expected,
        matched_patterns=matched,
        rejected_patterns=rejected,
        matched_rejections=matched_rejections,
        output_truncated=output_truncated,
    )


def _locked_entry(name: str, lock_path: Optional[Union[str, os.PathLike[str]]]) -> Mapping[str, object]:
    lock = load_firmware_lock(Path(lock_path) if lock_path is not None else None)
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, Mapping) or name not in artifacts:
        raise KeyError("unknown locked artifact %r" % name)
    entry = artifacts[name]
    if not isinstance(entry, Mapping):
        raise FirmwareFormatError("locked artifact %r is not an object" % name)
    digest = entry.get("sha256")
    url = entry.get("url")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FirmwareFormatError("locked artifact %r has an invalid SHA-256" % name)
    if not isinstance(url, str):
        raise FirmwareFormatError("locked artifact %r has no URL" % name)
    return entry


def locked_artifact_path(
    name: str,
    cache_directory: Union[str, os.PathLike[str]],
    lock_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> Path:
    """Return ``CACHE/sha256/DIGEST/BASENAME`` for a locked artifact."""

    entry = _locked_entry(name, lock_path)
    digest = str(entry["sha256"])
    parsed = urlparse(str(entry["url"]))
    basename = PurePosixPath(unquote(parsed.path)).name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+@-]*", basename or ""):
        basename = "artifact-" + digest[:12] + ".bin"
    return Path(cache_directory) / "sha256" / digest / basename


def verify_locked_artifact(
    name: str,
    path: Union[str, os.PathLike[str]],
    lock_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> str:
    """Verify the locked byte count and SHA-256 of a local artifact."""

    entry = _locked_entry(name, lock_path)
    artifact = Path(path)
    digest = hashlib.sha256()
    size = 0
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != entry["sha256"]:
        raise FirmwareFormatError(
            "%s digest %s, expected %s" % (name, actual, entry["sha256"])
        )
    expected_size = entry.get("bytes")
    if isinstance(expected_size, int) and size != expected_size:
        raise FirmwareFormatError(
            "%s has %d bytes, expected %d" % (name, size, expected_size)
        )
    return actual


def fetch_locked_to_cache(
    name: str,
    cache_directory: Union[str, os.PathLike[str]],
    lock_path: Optional[Union[str, os.PathLike[str]]] = None,
    *,
    force: bool = False,
) -> Path:
    """Fetch through the existing lock into a content-addressed cache."""

    destination = locked_artifact_path(name, cache_directory, lock_path)
    if destination.exists() and not force:
        verify_locked_artifact(name, destination, lock_path)
        return destination
    path = fetch_locked_artifact(
        name,
        destination,
        Path(lock_path) if lock_path is not None else None,
    )
    verify_locked_artifact(name, path, lock_path)
    return path


__all__ = [
    "BootArtifacts",
    "DEFAULT_BOOTARGS",
    "DEFAULT_FATAL_LOG_PATTERNS",
    "DEFAULT_LOG_PATTERNS",
    "MAX_BOOT_TIMEOUT_SECONDS",
    "MAX_FIT_BYTES",
    "MAX_QEMU_OUTPUT_BYTES",
    "QEMUBootResult",
    "build_qemu_command",
    "extract_boot_artifacts",
    "extract_fit_image",
    "extract_p210_bundle",
    "extract_pluto_archive",
    "fetch_locked_to_cache",
    "locate_qemu_system_arm",
    "locked_artifact_path",
    "run_qemu_boot",
    "verify_locked_artifact",
]
