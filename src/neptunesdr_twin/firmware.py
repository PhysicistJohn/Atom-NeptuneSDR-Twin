"""Parsers and compatibility checks for P210 and Pluto firmware artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
import urllib.request
import zlib

from .errors import FirmwareFormatError


FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9
UIMAGE_MAGIC = 0x27051956


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FDTNode:
    name: str
    parent: Optional["FDTNode"] = field(default=None, repr=False)
    properties: Dict[str, bytes] = field(default_factory=dict)
    children: List["FDTNode"] = field(default_factory=list)

    @property
    def path(self) -> str:
        if self.parent is None:
            return "/"
        parent = self.parent.path.rstrip("/")
        return parent + "/" + self.name

    def child(self, name: str) -> Optional["FDTNode"]:
        return next((item for item in self.children if item.name == name), None)

    def walk(self) -> Iterator["FDTNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def string_list(self, name: str) -> Tuple[str, ...]:
        value = self.properties.get(name)
        if value is None:
            return ()
        try:
            return tuple(part.decode("utf-8") for part in value.rstrip(b"\0").split(b"\0"))
        except UnicodeDecodeError as exc:
            raise FirmwareFormatError("%s:%s is not a UTF-8 string list" % (self.path, name)) from exc

    def string(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.string_list(name)
        return values[0] if values else default

    def u32s(self, name: str) -> Tuple[int, ...]:
        value = self.properties.get(name)
        if value is None:
            return ()
        if len(value) % 4:
            raise FirmwareFormatError("%s:%s is not an array of 32-bit cells" % (self.path, name))
        return struct.unpack(">" + "I" * (len(value) // 4), value)

    def u64(self, name: str) -> Optional[int]:
        cells = self.u32s(name)
        if not cells:
            return None
        if len(cells) == 1:
            return cells[0]
        if len(cells) != 2:
            raise FirmwareFormatError("%s:%s is not a scalar u64" % (self.path, name))
        return (cells[0] << 32) | cells[1]


@dataclass(frozen=True)
class FDTHeader:
    total_size: int
    structure_offset: int
    strings_offset: int
    reserve_map_offset: int
    version: int
    last_compatible_version: int
    boot_cpu_id: int
    strings_size: int
    structure_size: int


class FlattenedDeviceTree:
    """Bounds-checked flattened-device-tree/FIT parser."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 40:
            raise FirmwareFormatError("FDT is shorter than its fixed header")
        values = struct.unpack_from(">10I", data, 0)
        if values[0] != FDT_MAGIC:
            raise FirmwareFormatError("bad FDT magic 0x%08x" % values[0])
        self.header = FDTHeader(
            total_size=values[1],
            structure_offset=values[2],
            strings_offset=values[3],
            reserve_map_offset=values[4],
            version=values[5],
            last_compatible_version=values[6],
            boot_cpu_id=values[7],
            strings_size=values[8],
            structure_size=values[9],
        )
        if self.header.total_size > len(data):
            raise FirmwareFormatError("FDT total size exceeds artifact size")
        self.data = data[: self.header.total_size]
        self.trailer = data[self.header.total_size :]
        self._check_span(self.header.structure_offset, self.header.structure_size, "structure")
        self._check_span(self.header.strings_offset, self.header.strings_size, "strings")
        self._strings = self.data[
            self.header.strings_offset : self.header.strings_offset + self.header.strings_size
        ]
        self.root = self._parse_structure()

    def find(self, path: str) -> Optional[FDTNode]:
        if path == "/":
            return self.root
        current = self.root
        for name in (part for part in path.split("/") if part):
            current = current.child(name)
            if current is None:
                return None
        return current

    def compatible_nodes(self, compatible: str) -> Tuple[FDTNode, ...]:
        return tuple(node for node in self.root.walk() if compatible in node.string_list("compatible"))

    def _parse_structure(self) -> FDTNode:
        position = self.header.structure_offset
        limit = position + self.header.structure_size
        stack: List[FDTNode] = []
        root: Optional[FDTNode] = None
        saw_end = False
        while position + 4 <= limit:
            token = struct.unpack_from(">I", self.data, position)[0]
            position += 4
            if token == FDT_BEGIN_NODE:
                end = self.data.find(b"\0", position, limit)
                if end < 0:
                    raise FirmwareFormatError("unterminated FDT node name")
                name = self.data[position:end].decode("utf-8", errors="strict")
                position = self._align4(end + 1)
                parent = stack[-1] if stack else None
                node = FDTNode(name=name, parent=parent)
                if parent is not None:
                    parent.children.append(node)
                elif root is None:
                    root = node
                else:
                    raise FirmwareFormatError("FDT contains more than one root")
                stack.append(node)
            elif token == FDT_END_NODE:
                if not stack:
                    raise FirmwareFormatError("unbalanced FDT end-node token")
                stack.pop()
            elif token == FDT_PROP:
                if not stack or position + 8 > limit:
                    raise FirmwareFormatError("property outside node or truncated property header")
                length, name_offset = struct.unpack_from(">II", self.data, position)
                position += 8
                if position + length > limit:
                    raise FirmwareFormatError("FDT property data exceeds structure block")
                name = self._string_at(name_offset)
                stack[-1].properties[name] = self.data[position : position + length]
                position = self._align4(position + length)
            elif token == FDT_NOP:
                continue
            elif token == FDT_END:
                if stack:
                    raise FirmwareFormatError("FDT ended with unclosed nodes")
                saw_end = True
                break
            else:
                raise FirmwareFormatError("unknown FDT token %d" % token)
        if root is None or not saw_end:
            raise FirmwareFormatError("FDT has no complete root structure")
        return root

    def _string_at(self, offset: int) -> str:
        if offset >= len(self._strings):
            raise FirmwareFormatError("FDT string offset is out of bounds")
        end = self._strings.find(b"\0", offset)
        if end < 0:
            raise FirmwareFormatError("unterminated FDT property name")
        return self._strings[offset:end].decode("utf-8", errors="strict")

    def _check_span(self, offset: int, length: int, label: str) -> None:
        if offset < 0 or length < 0 or offset + length > self.header.total_size:
            raise FirmwareFormatError("FDT %s block is out of bounds" % label)

    @staticmethod
    def _align4(value: int) -> int:
        return (value + 3) & ~3


@dataclass(frozen=True)
class UImageHeader:
    header_crc: int
    timestamp: int
    data_size: int
    load_address: int
    entry_point: int
    data_crc: int
    os: int
    architecture: int
    image_type: int
    compression: int
    name: str


class UImage:
    """Legacy U-Boot image with both CRCs checked on construction."""

    HEADER_SIZE = 64

    def __init__(self, data: bytes, verify_crc: bool = True) -> None:
        if len(data) < self.HEADER_SIZE:
            raise FirmwareFormatError("uImage is shorter than its header")
        fields = struct.unpack_from(">7I4B32s", data, 0)
        if fields[0] != UIMAGE_MAGIC:
            raise FirmwareFormatError("bad uImage magic 0x%08x" % fields[0])
        self.header = UImageHeader(
            header_crc=fields[1],
            timestamp=fields[2],
            data_size=fields[3],
            load_address=fields[4],
            entry_point=fields[5],
            data_crc=fields[6],
            os=fields[7],
            architecture=fields[8],
            image_type=fields[9],
            compression=fields[10],
            name=fields[11].split(b"\0", 1)[0].decode("ascii", errors="replace"),
        )
        end = self.HEADER_SIZE + self.header.data_size
        if end > len(data):
            raise FirmwareFormatError("uImage payload is truncated")
        self.payload = data[self.HEADER_SIZE : end]
        self.trailer = data[end:]
        if verify_crc:
            header = bytearray(data[: self.HEADER_SIZE])
            header[4:8] = b"\0\0\0\0"
            if zlib.crc32(header) & 0xFFFFFFFF != self.header.header_crc:
                raise FirmwareFormatError("uImage header CRC mismatch")
            if zlib.crc32(self.payload) & 0xFFFFFFFF != self.header.data_crc:
                raise FirmwareFormatError("uImage data CRC mismatch")

    @property
    def timestamp_utc(self) -> str:
        return datetime.fromtimestamp(self.header.timestamp, timezone.utc).isoformat()


@dataclass(frozen=True)
class DFUSuffix:
    """USB DFU 1.0 suffix used by Pluto release artifacts."""

    bcd_device: int
    product_id: int
    vendor_id: int
    bcd_dfu: int
    length: int
    crc: int

    @classmethod
    def parse(cls, data: bytes, verify_crc: bool = True) -> "DFUSuffix":
        if len(data) < 16:
            raise FirmwareFormatError("DFU artifact is shorter than its suffix")
        bcd_device, product, vendor, bcd_dfu, signature, length, crc = struct.unpack(
            "<HHHH3sBI", data[-16:]
        )
        if signature != b"UFD" or length != 16:
            raise FirmwareFormatError("invalid DFU suffix signature or length")
        if verify_crc:
            expected = (~zlib.crc32(data[:-4])) & 0xFFFFFFFF
            if crc != expected:
                raise FirmwareFormatError("DFU suffix CRC mismatch")
        return cls(bcd_device, product, vendor, bcd_dfu, length, crc)


@dataclass(frozen=True)
class FirmwareIssue:
    severity: str
    check: str
    message: str


@dataclass
class FirmwareReport:
    source: str
    hashes: Dict[str, str] = field(default_factory=dict)
    facts: Dict[str, object] = field(default_factory=dict)
    issues: List[FirmwareIssue] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def add(self, severity: str, check: str, message: str) -> None:
        self.issues.append(FirmwareIssue(severity, check, message))

    def to_dict(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "compatible": self.compatible,
            "hashes": self.hashes,
            "facts": self.facts,
            "issues": [issue.__dict__ for issue in self.issues],
        }


class FirmwareBundle:
    """Read a firmware directory or tar archive without extracting it."""

    def __init__(self, source: Path) -> None:
        self.source = Path(source)
        self.files = self._read_files()

    def _read_files(self) -> Dict[str, bytes]:
        if self.source.is_dir():
            return {
                path.name: path.read_bytes()
                for path in self.source.iterdir()
                if path.is_file()
            }
        try:
            with tarfile.open(self.source, "r:*") as archive:
                result: Dict[str, bytes] = {}
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    result[Path(member.name).name] = handle.read()
                return result
        except tarfile.TarError as exc:
            raise FirmwareFormatError("not a readable firmware directory or tar archive") from exc


def validate_p210_firmware(source: Path) -> FirmwareReport:
    """Check the observed P210 SD boot contract and return extracted facts."""

    bundle = FirmwareBundle(source)
    report = FirmwareReport(str(source))
    required = {"BOOT.BIN", "uImage", "devicetree.dtb", "uEnv.txt"}
    missing = sorted(required - set(bundle.files))
    if missing:
        report.add("error", "bundle.members", "missing required files: " + ", ".join(missing))
        return report
    report.hashes = {name: sha256_bytes(data) for name, data in sorted(bundle.files.items())}

    try:
        kernel = UImage(bundle.files["uImage"])
        report.facts.update(
            {
                "kernel_name": kernel.header.name,
                "kernel_size": kernel.header.data_size,
                "kernel_load_address": kernel.header.load_address,
                "kernel_entry_point": kernel.header.entry_point,
                "kernel_timestamp_utc": kernel.timestamp_utc,
            }
        )
        if kernel.header.architecture != 2 or kernel.header.os != 5:
            report.add("error", "kernel.target", "uImage is not a Linux/ARM kernel")
    except FirmwareFormatError as exc:
        report.add("error", "kernel.integrity", str(exc))

    try:
        tree = FlattenedDeviceTree(bundle.files["devicetree.dtb"])
        root_compat = tree.root.string_list("compatible")
        report.facts["root_compatible"] = list(root_compat)
        if "xlnx,zynq-7000" not in root_compat:
            report.add("error", "dt.soc", "device tree does not target xlnx,zynq-7000")

        memory = tree.find("/memory")
        if memory is None:
            report.add("error", "dt.memory", "device tree has no /memory node")
        else:
            cells = memory.u32s("reg")
            if len(cells) >= 2:
                report.facts["ddr_base"] = cells[-2]
                report.facts["ddr_bytes"] = cells[-1]
            if len(cells) < 2 or cells[-1] != 0x20000000:
                report.add("error", "dt.memory", "expected observed P210 512 MiB DDR contract")

        radios = tree.compatible_nodes("adi,ad9361")
        if len(radios) != 1:
            report.add("error", "dt.ad9361", "expected exactly one adi,ad9361 node")
        else:
            radio = radios[0]
            mimo = "adi,2rx-2tx-mode-enable" in radio.properties
            report.facts["ad9361_2rx_2tx"] = mimo
            if not mimo:
                report.add("error", "dt.ad9361.mimo", "2Rx/2Tx mode is not enabled")

        expected_nodes = {
            "adi,axi-dmac-1.00.a": 2,
            "adi,axi-ad9361-6.00.a": 1,
            "adi,axi-ad9361-dds-6.00.a": 1,
            "cdns,zynq-gem": 1,
            "chipidea,usb2": 1,
        }
        observed_counts: Dict[str, int] = {}
        for compatible, minimum in expected_nodes.items():
            enabled = [
                node
                for node in tree.compatible_nodes(compatible)
                if node.string("status", "okay") != "disabled"
            ]
            observed_counts[compatible] = len(enabled)
            if len(enabled) < minimum:
                report.add(
                    "error",
                    "dt.peripheral." + compatible,
                    "expected at least %d enabled node(s), found %d" % (minimum, len(enabled)),
                )
        report.facts["enabled_compatible_counts"] = observed_counts
    except FirmwareFormatError as exc:
        report.add("error", "dt.integrity", str(exc))

    environment = bundle.files["uEnv.txt"].decode("utf-8", errors="replace")
    report.facts["bootargs"] = next(
        (line.partition("=")[2] for line in environment.splitlines() if line.startswith("bootargs=")),
        "",
    )
    if "root=/dev/mmcblk0p2" not in environment:
        report.add("error", "boot.sd_root", "uEnv does not select the P210 SD root partition")
    if len(bundle.files["BOOT.BIN"]) < 1_000_000:
        report.add("error", "boot.bin", "BOOT.BIN is implausibly small")
    return report


def validate_fit_image(data: bytes, source: str = "FIT image") -> FirmwareReport:
    """Validate a Pluto-style FIT container and all declared hash subnodes."""

    report = FirmwareReport(source)
    report.hashes[Path(source).name] = sha256_bytes(data)
    try:
        tree = FlattenedDeviceTree(data)
    except FirmwareFormatError as exc:
        report.add("error", "fit.structure", str(exc))
        return report
    if tree.trailer:
        if len(tree.trailer) == 33 and tree.trailer.endswith(b"\n"):
            expected = hashlib.md5(tree.data).hexdigest().encode("ascii")
            report.facts["frm_md5"] = tree.trailer[:-1].decode("ascii", errors="replace")
            if tree.trailer[:-1].lower() != expected:
                report.add("error", "fit.frm_md5", "FIT .frm MD5 trailer mismatch")
            report.add(
                "warning",
                "fit.authenticity",
                "legacy MD5 integrity is compatible but is not an authenticity signature",
            )
        elif len(tree.trailer) == 16 and tree.trailer[8:11] == b"UFD":
            try:
                suffix = DFUSuffix.parse(data)
                report.facts["dfu_suffix"] = {
                    "vendor_id": suffix.vendor_id,
                    "product_id": suffix.product_id,
                    "bcd_device": suffix.bcd_device,
                    "bcd_dfu": suffix.bcd_dfu,
                }
            except FirmwareFormatError as exc:
                report.add("error", "fit.dfu_suffix", str(exc))
        else:
            report.add("warning", "fit.trailer", "unrecognized bytes follow the FIT container")
    description = tree.root.string("description", "")
    report.facts["description"] = description
    images = tree.find("/images")
    configurations = tree.find("/configurations")
    if images is None or configurations is None:
        report.add("error", "fit.layout", "FIT lacks /images or /configurations")
        return report
    image_facts: Dict[str, object] = {}
    for image in images.children:
        payload = image.properties.get("data")
        if payload is None:
            report.add("error", "fit.image." + image.name, "image has no inline data")
            continue
        details: Dict[str, object] = {
            "bytes": len(payload),
            "type": image.string("type", ""),
            "compression": image.string("compression", ""),
            "sha256": sha256_bytes(payload),
        }
        image_facts[image.name] = details
        for hash_node in image.children:
            if hash_node.name.split("@", 1)[0] != "hash":
                continue
            algorithm_raw = hash_node.properties.get("algo")
            expected = hash_node.properties.get("value")
            check = "fit.hash." + image.name
            if algorithm_raw is None or expected is None:
                report.add(
                    "error",
                    check,
                    "%s is missing algo or value" % hash_node.path,
                )
                continue
            if (
                len(algorithm_raw) < 2
                or not algorithm_raw.endswith(b"\0")
                or b"\0" in algorithm_raw[:-1]
            ):
                report.add(
                    "error",
                    check,
                    "%s has a malformed scalar hash algorithm" % hash_node.path,
                )
                continue
            try:
                algorithm = algorithm_raw[:-1].decode("ascii")
            except UnicodeDecodeError:
                report.add(
                    "error",
                    check,
                    "%s hash algorithm is not ASCII" % hash_node.path,
                )
                continue
            try:
                digest = hashlib.new(algorithm, payload)
            except (TypeError, ValueError):
                report.add("error", check, "unsupported hash " + algorithm)
                continue
            if digest.digest_size <= 0:
                report.add("error", check, "unsupported variable-length hash " + algorithm)
                continue
            if len(expected) != digest.digest_size:
                report.add(
                    "error",
                    check,
                    "%s value length %d does not match %s digest length %d"
                    % (hash_node.path, len(expected), algorithm, digest.digest_size),
                )
                continue
            actual = digest.digest()
            if actual != expected:
                report.add("error", check, algorithm + " mismatch")
    report.facts["images"] = image_facts
    report.facts["configurations"] = [child.name for child in configurations.children]
    return report


def load_firmware_lock(path: Optional[Path] = None) -> Mapping[str, object]:
    if path is None:
        path = Path(__file__).with_name("data") / "firmware-lock.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_locked_artifact(name: str, destination: Path, lock_path: Optional[Path] = None) -> Path:
    """Download one content-addressed artifact, refusing an unexpected digest."""

    lock = load_firmware_lock(lock_path)
    artifacts = lock.get("artifacts", {})
    if name not in artifacts:
        raise KeyError("unknown locked artifact %r" % name)
    entry = artifacts[name]
    url = entry["url"]
    expected = entry["sha256"]
    expected_bytes = int(entry["bytes"])
    if expected_bytes < 0:
        raise FirmwareFormatError("locked artifact size cannot be negative")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_bytes:
                    raise FirmwareFormatError(
                        "downloaded %s exceeds its locked size %d" % (name, expected_bytes)
                    )
                output.write(chunk)
                digest.update(chunk)
        if received != expected_bytes:
            raise FirmwareFormatError(
                "downloaded %s has %d bytes, expected %d"
                % (name, received, expected_bytes)
            )
        if digest.hexdigest() != expected:
            raise FirmwareFormatError(
                "downloaded %s digest %s, expected %s" % (name, digest.hexdigest(), expected)
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
