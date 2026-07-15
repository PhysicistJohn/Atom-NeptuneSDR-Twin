"""Provenance-preserving ARM userspace support for the P210 boot harness.

This module does not manufacture a replacement firmware.  It combines two
already content-addressed public inputs for a narrowly stated experiment:

* the public P210 Linux kernel and device tree; and
* the unmodified initramfs embedded in Analog Devices' Pluto v0.39 release.

The combination is an ABI-qualified runtime candidate, not vendor P210
firmware.  The distinction is carried in :class:`RuntimeCandidate` so callers
cannot accidentally report an artifact-integrity check as a hardware boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import struct
import tempfile
import zlib
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .boot_harness import (
    DEFAULT_BOOTARGS,
    BootArtifacts,
    extract_p210_bundle,
    extract_pluto_archive,
)
from .errors import FirmwareFormatError
from .firmware import FirmwareBundle, FlattenedDeviceTree, UImage, sha256_bytes


MAX_ROOTFS_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_CPIO_ENTRIES = 65536
MAX_CPIO_NAME_BYTES = 4096
MAX_KERNEL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
NEWC_MAGIC = b"070701"
NEWC_CRC_MAGIC = b"070702"
TRAILER_NAME = "TRAILER!!!"
EM_ARM = 40
ELFCLASS32 = 1
ELFDATA2LSB = 1
EF_ARM_EABIMASK = 0xFF000000
EF_ARM_ABI_FLOAT_SOFT = 0x00000200
EF_ARM_ABI_FLOAT_HARD = 0x00000400
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
PT_NOTE = 4
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _bounded_gunzip(data: bytes) -> bytes:
    """Inflate one gzip payload while enforcing an uncompressed-size bound."""

    try:
        stream = gzip.GzipFile(fileobj=_BytesReader(data), mode="rb")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = stream.read(min(1024 * 1024, MAX_ROOTFS_UNCOMPRESSED_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ROOTFS_UNCOMPRESSED_BYTES:
                raise FirmwareFormatError("rootfs expands beyond the 128 MiB audit limit")
            chunks.append(chunk)
        return b"".join(chunks)
    except (OSError, EOFError) as exc:
        raise FirmwareFormatError("rootfs is not a complete gzip stream") from exc


class _BytesReader:
    """Small seekable reader used to keep the module dependency-free."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._offset
        result = self._data[self._offset : self._offset + size]
        self._offset += len(result)
        return result

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self._offset + offset
        elif whence == 2:
            target = len(self._data) + offset
        else:
            raise ValueError("invalid whence")
        if target < 0:
            raise ValueError("negative seek")
        self._offset = min(target, len(self._data))
        return self._offset

    def tell(self) -> int:
        return self._offset


@dataclass(frozen=True)
class CpioEntry:
    name: str
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    mtime: int
    data: bytes
    devmajor: int = 0
    devminor: int = 0
    rdevmajor: int = 0
    rdevminor: int = 0

    @property
    def is_regular(self) -> bool:
        return self.mode & 0o170000 == 0o100000

    @property
    def is_symlink(self) -> bool:
        return self.mode & 0o170000 == 0o120000


class NewcArchive:
    """Bounded parser/writer for the kernel's ASCII ``newc`` initramfs."""

    def __init__(self, entries: Sequence[CpioEntry]) -> None:
        self.entries = tuple(entries)
        for entry in self.entries:
            self._validate_name(entry.name)
        self._by_name = {entry.name: entry for entry in self.entries}
        if len(self._by_name) != len(self.entries):
            raise FirmwareFormatError("rootfs cpio contains duplicate path records")

    @classmethod
    def parse(cls, data: bytes) -> "NewcArchive":
        offset = 0
        entries: List[CpioEntry] = []
        saw_trailer = False
        while offset < len(data):
            # Linux initramfs archives can be padded with NULs after the trailer.
            if data[offset:] and not data[offset:].strip(b"\0"):
                break
            if len(entries) >= MAX_CPIO_ENTRIES:
                raise FirmwareFormatError("rootfs cpio exceeds the entry limit")
            if offset + 110 > len(data):
                raise FirmwareFormatError("truncated rootfs cpio header")
            magic = data[offset : offset + 6]
            if magic not in (NEWC_MAGIC, NEWC_CRC_MAGIC):
                raise FirmwareFormatError("rootfs is not an ASCII newc cpio archive")
            raw_fields = data[offset + 6 : offset + 110]
            try:
                fields = tuple(
                    int(raw_fields[index : index + 8], 16)
                    for index in range(0, len(raw_fields), 8)
                )
            except ValueError as exc:
                raise FirmwareFormatError("rootfs cpio has a non-hex header field") from exc
            if len(fields) != 13:
                raise FirmwareFormatError("rootfs cpio header field count is invalid")
            (
                inode,
                mode,
                uid,
                gid,
                nlink,
                mtime,
                filesize,
                devmajor,
                devminor,
                rdevmajor,
                rdevminor,
                namesize,
                checksum,
            ) = fields
            if not 1 <= namesize <= MAX_CPIO_NAME_BYTES:
                raise FirmwareFormatError("rootfs cpio path length is invalid")
            name_start = offset + 110
            name_end = name_start + namesize
            if name_end > len(data) or data[name_end - 1] != 0:
                raise FirmwareFormatError("rootfs cpio path is truncated or unterminated")
            try:
                name = data[name_start : name_end - 1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FirmwareFormatError("rootfs cpio path is not UTF-8") from exc
            data_start = _align4(name_end)
            data_end = data_start + filesize
            if data_end > len(data):
                raise FirmwareFormatError("rootfs cpio file payload is truncated")
            payload = data[data_start:data_end]
            if magic == NEWC_CRC_MAGIC and sum(payload) & 0xFFFFFFFF != checksum:
                raise FirmwareFormatError("rootfs cpio payload checksum mismatch")
            offset = _align4(data_end)
            if name == TRAILER_NAME:
                saw_trailer = True
                break
            cls._validate_name(name)
            entries.append(
                CpioEntry(
                    name=name,
                    inode=inode,
                    mode=mode,
                    uid=uid,
                    gid=gid,
                    nlink=nlink,
                    mtime=mtime,
                    data=payload,
                    devmajor=devmajor,
                    devminor=devminor,
                    rdevmajor=rdevmajor,
                    rdevminor=rdevminor,
                )
            )
        if not saw_trailer:
            raise FirmwareFormatError("rootfs cpio has no TRAILER!!! record")
        return cls(entries)

    @staticmethod
    def _validate_name(name: str) -> None:
        if name in ("", "."):
            return
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ("", "..") for part in path.parts):
            raise FirmwareFormatError("unsafe rootfs cpio path %r" % name)
        if "\0" in name or "\\" in name:
            raise FirmwareFormatError("unsafe rootfs cpio path %r" % name)

    @classmethod
    def from_gzip(cls, data: bytes) -> "NewcArchive":
        return cls.parse(_bounded_gunzip(data))

    def entry(self, path: str) -> CpioEntry:
        normalized = path.lstrip("/") or "."
        try:
            return self._by_name[normalized]
        except KeyError as exc:
            raise FirmwareFormatError("rootfs lacks /%s" % normalized) from exc

    def read(self, path: str, *, follow_symlinks: bool = True) -> bytes:
        normalized = path.lstrip("/") or "."
        seen = set()
        for _ in range(16):
            entry = self.entry(normalized)
            if not follow_symlinks or not entry.is_symlink:
                return entry.data
            if normalized in seen:
                raise FirmwareFormatError("rootfs symlink loop at /%s" % normalized)
            seen.add(normalized)
            try:
                target = entry.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FirmwareFormatError("rootfs symlink target is not UTF-8") from exc
            if target.startswith("/"):
                candidate = target.lstrip("/")
            else:
                candidate = os.fspath(PurePosixPath(normalized).parent / target)
            parts: List[str] = []
            for part in PurePosixPath(candidate).parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise FirmwareFormatError("rootfs symlink escapes archive root")
                    parts.pop()
                else:
                    parts.append(part)
            normalized = "/".join(parts) or "."
        raise FirmwareFormatError("rootfs symlink depth exceeds 16")

    def contains(self, path: str, *, follow_symlinks: bool = True) -> bool:
        try:
            self.read(path, follow_symlinks=follow_symlinks)
            return True
        except FirmwareFormatError:
            return False

    def replaced(self, path: str, data: bytes, *, mode: Optional[int] = None) -> "NewcArchive":
        normalized = path.lstrip("/")
        original = self.entry(normalized)
        replacement = replace(original, data=bytes(data), mode=original.mode if mode is None else mode)
        return NewcArchive(
            tuple(replacement if entry.name == normalized else entry for entry in self.entries)
        )

    def added(self, entry: CpioEntry) -> "NewcArchive":
        """Append one entry, rejecting ambiguous duplicate paths."""

        self._validate_name(entry.name)
        if entry.name in self._by_name:
            raise FirmwareFormatError("rootfs already contains /%s" % entry.name)
        return NewcArchive(self.entries + (entry,))

    def to_bytes(self) -> bytes:
        result = bytearray()
        for entry in self.entries:
            self._append_entry(result, entry)
        trailer_inode = max((entry.inode for entry in self.entries), default=0) + 1
        self._append_entry(
            result,
            CpioEntry(
                TRAILER_NAME,
                trailer_inode,
                0,
                0,
                0,
                1,
                0,
                b"",
            ),
        )
        result.extend(b"\0" * ((512 - (len(result) % 512)) % 512))
        return bytes(result)

    @staticmethod
    def _append_entry(output: bytearray, entry: CpioEntry) -> None:
        name = entry.name.encode("utf-8") + b"\0"
        values = (
            entry.inode,
            entry.mode,
            entry.uid,
            entry.gid,
            entry.nlink,
            entry.mtime,
            len(entry.data),
            entry.devmajor,
            entry.devminor,
            entry.rdevmajor,
            entry.rdevminor,
            len(name),
            0,
        )
        output.extend(NEWC_MAGIC)
        for value in values:
            if not 0 <= value <= 0xFFFFFFFF:
                raise FirmwareFormatError("cpio metadata value is outside uint32")
            output.extend(("%08X" % value).encode("ascii"))
        output.extend(name)
        output.extend(b"\0" * ((-len(output)) % 4))
        output.extend(entry.data)
        output.extend(b"\0" * ((-len(output)) % 4))

    def to_gzip(self) -> bytes:
        return gzip.compress(self.to_bytes(), compresslevel=9, mtime=0)


@dataclass(frozen=True)
class ELFProgramHeader:
    kind: int
    offset: int
    virtual_address: int
    file_size: int
    memory_size: int


class ELF32ARM:
    """Minimal ELF reader for proving the initramfs executable ABI."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 52 or data[:4] != b"\x7fELF":
            raise FirmwareFormatError("rootfs executable is not ELF")
        if data[4] != ELFCLASS32 or data[5] != ELFDATA2LSB:
            raise FirmwareFormatError("rootfs executable is not 32-bit little-endian ELF")
        header = struct.unpack_from("<16sHHIIIIIHHHHHH", data, 0)
        self.machine = header[2]
        self.flags = header[7]
        phoff, phentsize, phnum = header[5], header[9], header[10]
        if self.machine != EM_ARM:
            raise FirmwareFormatError("rootfs executable is not ARM ELF")
        if phentsize < 32 or phnum > 256 or phoff + phentsize * phnum > len(data):
            raise FirmwareFormatError("rootfs ELF program headers are invalid")
        headers: List[ELFProgramHeader] = []
        for index in range(phnum):
            fields = struct.unpack_from("<IIIIIIII", data, phoff + index * phentsize)
            kind, offset, virtual, _physical, file_size, memory_size, _flags, _align = fields
            if offset + file_size > len(data):
                raise FirmwareFormatError("rootfs ELF segment is truncated")
            headers.append(ELFProgramHeader(kind, offset, virtual, file_size, memory_size))
        self._data = data
        self.program_headers = tuple(headers)

    @property
    def eabi_version(self) -> int:
        return (self.flags & EF_ARM_EABIMASK) >> 24

    @property
    def float_abi(self) -> str:
        hard = bool(self.flags & EF_ARM_ABI_FLOAT_HARD)
        soft = bool(self.flags & EF_ARM_ABI_FLOAT_SOFT)
        if hard and soft:
            return "conflicting"
        if hard:
            return "hard"
        if soft:
            return "soft"
        return "unspecified"

    @property
    def interpreter(self) -> Optional[str]:
        segments = [segment for segment in self.program_headers if segment.kind == PT_INTERP]
        if not segments:
            return None
        if len(segments) != 1:
            raise FirmwareFormatError("rootfs ELF has multiple interpreters")
        raw = self._segment(segments[0])
        if not raw.endswith(b"\0"):
            raise FirmwareFormatError("rootfs ELF interpreter is unterminated")
        return raw[:-1].decode("utf-8")

    def _segment(self, segment: ELFProgramHeader) -> bytes:
        return self._data[segment.offset : segment.offset + segment.file_size]

    def _virtual_to_offset(self, address: int, length: int = 1) -> int:
        for segment in self.program_headers:
            if segment.kind != PT_LOAD:
                continue
            delta = address - segment.virtual_address
            if 0 <= delta and delta + length <= segment.file_size:
                return segment.offset + delta
        raise FirmwareFormatError("rootfs ELF virtual address has no file mapping")

    @property
    def needed(self) -> Tuple[str, ...]:
        dynamic = [segment for segment in self.program_headers if segment.kind == PT_DYNAMIC]
        if not dynamic:
            return ()
        if len(dynamic) != 1 or dynamic[0].file_size % 8:
            raise FirmwareFormatError("rootfs ELF dynamic table is malformed")
        needed_offsets: List[int] = []
        string_address: Optional[int] = None
        string_size: Optional[int] = None
        for offset in range(dynamic[0].offset, dynamic[0].offset + dynamic[0].file_size, 8):
            tag, value = struct.unpack_from("<II", self._data, offset)
            if tag == DT_NULL:
                break
            if tag == DT_NEEDED:
                needed_offsets.append(value)
            elif tag == DT_STRTAB:
                string_address = value
            elif tag == DT_STRSZ:
                string_size = value
        if needed_offsets and (string_address is None or string_size is None):
            raise FirmwareFormatError("rootfs ELF lacks its dynamic string table")
        if string_address is None or string_size is None:
            return ()
        start = self._virtual_to_offset(string_address, string_size)
        table = self._data[start : start + string_size]
        result = []
        for offset in needed_offsets:
            if offset >= len(table):
                raise FirmwareFormatError("rootfs ELF DT_NEEDED offset is invalid")
            end = table.find(b"\0", offset)
            if end < 0:
                raise FirmwareFormatError("rootfs ELF DT_NEEDED name is unterminated")
            result.append(table[offset:end].decode("utf-8"))
        return tuple(result)

    @property
    def linux_abi_floor(self) -> Optional[Tuple[int, int, int]]:
        for segment in self.program_headers:
            if segment.kind != PT_NOTE:
                continue
            raw = self._segment(segment)
            offset = 0
            while offset + 12 <= len(raw):
                namesize, descsize, kind = struct.unpack_from("<III", raw, offset)
                offset += 12
                name_end = offset + namesize
                if name_end > len(raw):
                    break
                name = raw[offset:name_end]
                offset = _align4(name_end)
                desc_end = offset + descsize
                if desc_end > len(raw):
                    break
                description = raw[offset:desc_end]
                offset = _align4(desc_end)
                if name.rstrip(b"\0") == b"GNU" and kind == 1 and len(description) >= 16:
                    os_id, major, minor, patch = struct.unpack_from("<IIII", description, 0)
                    if os_id == 0:
                        return major, minor, patch
        return None


@dataclass(frozen=True)
class RootfsAudit:
    sha256: str
    compressed_bytes: int
    uncompressed_bytes: int
    entries: int
    iiod_sha256: str
    libiio_sha256: str
    libc_sha256: str
    loader_sha256: str
    iiod_interpreter: str
    iiod_needed: Tuple[str, ...]
    arm_eabi: int
    float_abi: str
    linux_abi_floor: Tuple[int, int, int]
    libiio_version: str
    firmware_version: str
    iiod_functionfs_endpoints: int
    usb_vid: int
    usb_pid: int
    service_command: str
    file_hashes: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "sha256": self.sha256,
            "compressed_bytes": self.compressed_bytes,
            "uncompressed_bytes": self.uncompressed_bytes,
            "entries": self.entries,
            "iiod_sha256": self.iiod_sha256,
            "libiio_sha256": self.libiio_sha256,
            "libc_sha256": self.libc_sha256,
            "loader_sha256": self.loader_sha256,
            "iiod_interpreter": self.iiod_interpreter,
            "iiod_needed": list(self.iiod_needed),
            "arm_eabi": self.arm_eabi,
            "float_abi": self.float_abi,
            "linux_abi_floor": list(self.linux_abi_floor),
            "libiio_version": self.libiio_version,
            "firmware_version": self.firmware_version,
            "iiod_functionfs_endpoints": self.iiod_functionfs_endpoints,
            "usb_vid": self.usb_vid,
            "usb_pid": self.usb_pid,
            "service_command": self.service_command,
            "file_hashes": dict(self.file_hashes),
        }


P210_REQUIRED_KERNEL_CONFIG = (
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_RD_GZIP",
    "CONFIG_DEVTMPFS",
    "CONFIG_DEVTMPFS_MOUNT",
    "CONFIG_NET",
    "CONFIG_MACB",
    "CONFIG_USB_CHIPIDEA",
    "CONFIG_USB_GADGET",
    "CONFIG_USB_CONFIGFS",
    "CONFIG_USB_CONFIGFS_F_FS",
    "CONFIG_MMC",
    "CONFIG_MMC_SDHCI",
    "CONFIG_MMC_SDHCI_OF_ARASAN",
    "CONFIG_AXI_DMAC",
    "CONFIG_IIO",
    "CONFIG_IIO_BUFFER_DMA",
    "CONFIG_AD9361",
    "CONFIG_EXT4_FS",
)


@dataclass(frozen=True)
class KernelAudit:
    name: str
    version: Tuple[int, int, int]
    build_string: str
    payload_sha256: str
    uncompressed_sha256: str
    config_sha256: str
    config_bytes: int
    required_config: Mapping[str, str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "version": list(self.version),
            "build_string": self.build_string,
            "payload_sha256": self.payload_sha256,
            "uncompressed_sha256": self.uncompressed_sha256,
            "config_sha256": self.config_sha256,
            "config_bytes": self.config_bytes,
            "required_config": dict(self.required_config),
        }


@dataclass(frozen=True)
class DeviceTreeRuntimeAudit:
    sha256: str
    compatible: Tuple[str, ...]
    memory_bytes: int
    ethernet_path: str
    ethernet_status: str
    ethernet_phy_mode: str
    ethernet_phy_address: int
    usb_path: str
    usb_status: str
    usb_dr_mode: str
    hardware_tcp_possible: bool
    hardware_usb_gadget_possible: bool
    qemu_gem_phy_matches: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "sha256": self.sha256,
            "compatible": list(self.compatible),
            "memory_bytes": self.memory_bytes,
            "ethernet": {
                "path": self.ethernet_path,
                "status": self.ethernet_status,
                "phy_mode": self.ethernet_phy_mode,
                "phy_address": self.ethernet_phy_address,
                "hardware_tcp_possible": self.hardware_tcp_possible,
                "stock_qemu_expected_phy_address": 7,
                "stock_qemu_phy_matches": self.qemu_gem_phy_matches,
            },
            "usb": {
                "path": self.usb_path,
                "status": self.usb_status,
                "dr_mode": self.usb_dr_mode,
                "hardware_gadget_possible": self.hardware_usb_gadget_possible,
            },
        }


def audit_p210_runtime_dtb(data: bytes) -> DeviceTreeRuntimeAudit:
    tree = FlattenedDeviceTree(data)
    compatible = tree.root.string_list("compatible")
    if "xlnx,zynq-7000" not in compatible:
        raise FirmwareFormatError("P210 runtime DT is not a Zynq-7000 tree")
    memory_nodes = [node for node in tree.root.walk() if node.properties.get("device_type") == b"memory\0"]
    if len(memory_nodes) != 1:
        raise FirmwareFormatError("P210 runtime DT must have one memory node")
    memory_cells = memory_nodes[0].u32s("reg")
    if len(memory_cells) != 2:
        raise FirmwareFormatError("P210 runtime DT memory range is not two 32-bit cells")

    ethernet_nodes = tree.compatible_nodes("cdns,zynq-gem")
    enabled_ethernet = [node for node in ethernet_nodes if node.string("status", "okay") == "okay"]
    if len(enabled_ethernet) != 1:
        raise FirmwareFormatError("P210 runtime DT must enable exactly one Zynq GEM")
    ethernet = enabled_ethernet[0]
    handles = ethernet.u32s("phy-handle")
    if len(handles) != 1:
        raise FirmwareFormatError("P210 runtime DT GEM lacks one phy-handle")
    phy = next(
        (
            node
            for node in tree.root.walk()
            if handles[0] in node.u32s("phandle") or handles[0] in node.u32s("linux,phandle")
        ),
        None,
    )
    if phy is None or len(phy.u32s("reg")) != 1:
        raise FirmwareFormatError("P210 runtime DT GEM PHY handle is unresolved")
    phy_address = phy.u32s("reg")[0]

    usb_nodes = tree.compatible_nodes("chipidea,usb2")
    enabled_usb = [node for node in usb_nodes if node.string("status", "okay") == "okay"]
    if len(enabled_usb) != 1:
        raise FirmwareFormatError("P210 runtime DT must enable exactly one USB controller")
    usb = enabled_usb[0]
    usb_mode = usb.string("dr_mode", "otg") or "otg"
    ethernet_status = ethernet.string("status", "okay") or "okay"
    usb_status = usb.string("status", "okay") or "okay"
    return DeviceTreeRuntimeAudit(
        sha256=_sha(data),
        compatible=compatible,
        memory_bytes=memory_cells[1],
        ethernet_path=ethernet.path,
        ethernet_status=ethernet_status,
        ethernet_phy_mode=ethernet.string("phy-mode", "unknown") or "unknown",
        ethernet_phy_address=phy_address,
        usb_path=usb.path,
        usb_status=usb_status,
        usb_dr_mode=usb_mode,
        hardware_tcp_possible=ethernet_status == "okay",
        hardware_usb_gadget_possible=usb_status == "okay" and usb_mode in ("otg", "peripheral"),
        qemu_gem_phy_matches=phy_address == 7,
    )


def _gunzip_member(data: bytes, offset: int, limit: int) -> bytes:
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        result = inflater.decompress(data[offset:], limit + 1)
        if len(result) > limit or inflater.unconsumed_tail:
            raise FirmwareFormatError("compressed kernel member exceeds audit limit")
        if not inflater.eof:
            raise FirmwareFormatError("compressed kernel member is incomplete")
        return result + inflater.flush()
    except zlib.error as exc:
        raise FirmwareFormatError("compressed kernel member is invalid") from exc


def audit_p210_kernel_payload(payload: bytes, name: str) -> KernelAudit:
    """Extract the public zImage and its IKCONFIG preconditions in-process."""

    candidates: List[int] = []
    start = 0
    while len(candidates) < 64:
        offset = payload.find(b"\x1f\x8b\x08", start)
        if offset < 0:
            break
        candidates.append(offset)
        start = offset + 1
    raw_kernel: Optional[bytes] = None
    for offset in candidates:
        try:
            expanded = _gunzip_member(payload, offset, MAX_KERNEL_UNCOMPRESSED_BYTES)
        except FirmwareFormatError:
            continue
        if b"Linux version " in expanded and b"IKCFG_ST" in expanded:
            raw_kernel = expanded
            break
    if raw_kernel is None:
        raise FirmwareFormatError("P210 zImage has no auditable gzip kernel/IKCONFIG member")

    build_match = re.search(rb"Linux version ([^\0\n]+)", raw_kernel)
    if build_match is None:
        raise FirmwareFormatError("P210 kernel build string is absent")
    build_string = "Linux version " + build_match.group(1).decode("utf-8", errors="replace")
    version_match = re.search(r"Linux version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", build_string)
    if version_match is None:
        raise FirmwareFormatError("P210 kernel build string has no version")
    version = tuple(int(value or 0) for value in version_match.groups())

    marker = raw_kernel.find(b"IKCFG_ST")
    config_offset = raw_kernel.find(b"\x1f\x8b\x08", marker + len(b"IKCFG_ST"))
    if config_offset < 0:
        raise FirmwareFormatError("P210 IKCONFIG marker has no gzip payload")
    config = _gunzip_member(raw_kernel, config_offset, 4 * 1024 * 1024)
    try:
        config_text = config.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FirmwareFormatError("P210 IKCONFIG is not UTF-8") from exc
    values: Dict[str, str] = {}
    for line in config_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith("CONFIG_"):
            values[key] = value
    missing = [key for key in P210_REQUIRED_KERNEL_CONFIG if values.get(key) != "y"]
    if missing:
        raise FirmwareFormatError(
            "P210 kernel lacks required built-in config: %s" % ", ".join(missing)
        )
    name_match = re.search(r"Linux-([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", name)
    if name_match is None:
        raise FirmwareFormatError("P210 uImage name has no Linux version")
    name_version = tuple(int(value or 0) for value in name_match.groups())
    if name_version != version:
        raise FirmwareFormatError("P210 uImage name and embedded kernel version disagree")
    return KernelAudit(
        name=name,
        version=version,
        build_string=build_string,
        payload_sha256=_sha(payload),
        uncompressed_sha256=_sha(raw_kernel),
        config_sha256=_sha(config),
        config_bytes=len(config),
        required_config={key: values[key] for key in P210_REQUIRED_KERNEL_CONFIG},
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(archive: NewcArchive, path: str) -> str:
    try:
        return archive.read(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FirmwareFormatError("rootfs %s is not UTF-8" % path) from exc


def _assignment(text: str, name: str) -> str:
    match = re.search(r"(?m)^%s=(?:\"([^\"]*)\"|'([^']*)'|([^\s#]+))\s*$" % re.escape(name), text)
    if match is None:
        raise FirmwareFormatError("rootfs configuration lacks %s" % name)
    return next(value for value in match.groups() if value is not None)


def _resolve_library(archive: NewcArchive, name: str) -> Optional[str]:
    if "/" in name:
        return name if archive.contains(name) else None
    for directory in ("/lib", "/usr/lib"):
        candidate = directory + "/" + name
        if archive.contains(candidate):
            return candidate
    return None


def audit_pluto_rootfs(rootfs_gzip: Union[bytes, bytearray, memoryview, str, os.PathLike[str]]) -> RootfsAudit:
    """Prove that a Pluto initramfs contains an executable ARM ``iiod`` stack."""

    if isinstance(rootfs_gzip, (bytes, bytearray, memoryview)):
        compressed = bytes(rootfs_gzip)
    else:
        compressed = Path(rootfs_gzip).read_bytes()
    uncompressed = _bounded_gunzip(compressed)
    archive = NewcArchive.parse(uncompressed)
    init = _text(archive, "/init")
    if not init.startswith("#!/bin/sh") or not archive.contains("/sbin/init"):
        raise FirmwareFormatError("rootfs init contract is incomplete")

    iiod_data = archive.read("/usr/sbin/iiod")
    iiod = ELF32ARM(iiod_data)
    if iiod.eabi_version != 5 or iiod.float_abi != "hard":
        raise FirmwareFormatError("iiod is not ARM EABI5 hard-float")
    interpreter = iiod.interpreter
    if interpreter is None or not archive.contains(interpreter):
        raise FirmwareFormatError("iiod dynamic loader is absent from rootfs")
    unresolved = [name for name in iiod.needed if _resolve_library(archive, name) is None]
    if unresolved:
        raise FirmwareFormatError("iiod dependencies are absent: %s" % ", ".join(unresolved))

    libc_data = archive.read("/lib/libc.so.6")
    libc = ELF32ARM(libc_data)
    abi_floor = libc.linux_abi_floor
    if abi_floor is None:
        raise FirmwareFormatError("libc has no GNU/Linux ABI tag")
    loader_data = archive.read(interpreter)
    libiio_path = _resolve_library(archive, "libiio.so.0")
    if libiio_path is None:
        raise FirmwareFormatError("rootfs lacks libiio.so.0")
    libiio_data = archive.read(libiio_path)

    versions = _text(archive, "/opt/VERSIONS")
    firmware_match = re.search(r"(?m)^device-fw\s+(\S+)$", versions)
    libiio_match = re.search(rb"libiio version: ([0-9]+\.[0-9]+)", libiio_data)
    if firmware_match is None:
        raise FirmwareFormatError("rootfs VERSIONS lacks device-fw")
    # Stripped builds do not all retain the literal; the SONAME target is the
    # release version authority in that case.
    if libiio_match is not None:
        libiio_version = libiio_match.group(1).decode("ascii")
    else:
        target = archive.entry("usr/lib/libiio.so.0")
        if not target.is_symlink:
            raise FirmwareFormatError("libiio version cannot be established")
        target_name = target.data.decode("ascii")
        match = re.search(r"\.so\.([0-9]+\.[0-9]+)$", target_name)
        if match is None:
            raise FirmwareFormatError("libiio SONAME target has no version")
        libiio_version = match.group(1)

    device_config = _text(archive, "/etc/device_config")
    usb_pid = int(_assignment(device_config, "USBPID"), 0)
    endpoints = int(_assignment(device_config, "ENDPOINTS"), 0)
    udc = _text(archive, "/etc/init.d/S23udc")
    command_match = re.search(r"IIOD_OPTS=\"([^\"]+)\"", udc)
    if command_match is None or "/usr/sbin/iiod" not in udc:
        raise FirmwareFormatError("rootfs does not start iiod from S23udc")
    service_command = "/usr/sbin/iiod " + command_match.group(1).replace("$ENDPOINTS", str(endpoints))
    if "-F /dev/iio_ffs" not in service_command:
        raise FirmwareFormatError("rootfs iiod service is not bound to IIO FunctionFS")

    selected = {
        "/init": init.encode("utf-8"),
        "/usr/sbin/iiod": iiod_data,
        "/usr/lib/libiio.so.0": libiio_data,
        "/lib/libc.so.6": libc_data,
        interpreter: loader_data,
        "/etc/device_config": device_config.encode("utf-8"),
        "/etc/init.d/S23udc": udc.encode("utf-8"),
        "/opt/VERSIONS": versions.encode("utf-8"),
    }
    return RootfsAudit(
        sha256=_sha(compressed),
        compressed_bytes=len(compressed),
        uncompressed_bytes=len(uncompressed),
        entries=len(archive.entries),
        iiod_sha256=_sha(iiod_data),
        libiio_sha256=_sha(libiio_data),
        libc_sha256=_sha(libc_data),
        loader_sha256=_sha(loader_data),
        iiod_interpreter=interpreter,
        iiod_needed=iiod.needed,
        arm_eabi=iiod.eabi_version,
        float_abi=iiod.float_abi,
        linux_abi_floor=abi_floor,
        libiio_version=libiio_version,
        firmware_version=firmware_match.group(1),
        iiod_functionfs_endpoints=endpoints,
        usb_vid=0x0456,
        usb_pid=usb_pid,
        service_command=service_command,
        file_hashes={path: _sha(data) for path, data in selected.items()},
    )


@dataclass(frozen=True)
class RuntimeCandidate:
    artifacts: BootArtifacts
    kernel: KernelAudit
    devicetree: DeviceTreeRuntimeAudit
    rootfs: RootfsAudit
    classification: str
    kernel_name: str
    abi_compatible: bool
    provenance: Mapping[str, str]
    limitations: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "classification": self.classification,
            "abi_compatible": self.abi_compatible,
            "kernel_name": self.kernel_name,
            "kernel": self.kernel.to_dict(),
            "devicetree": self.devicetree.to_dict(),
            "provenance": dict(self.provenance),
            "limitations": list(self.limitations),
            "rootfs": self.rootfs.to_dict(),
            "boot": {
                "kind": self.artifacts.kind,
                "kernel": os.fspath(self.artifacts.kernel),
                "devicetree": os.fspath(self.artifacts.devicetree),
                "ramdisk": os.fspath(self.artifacts.ramdisk) if self.artifacts.ramdisk else None,
                "bootargs": self.artifacts.bootargs,
                "hashes": dict(self.artifacts.hashes),
                "execution_scope": self.artifacts.execution_scope,
            },
        }


def _atomic_copy(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    temporary = Path(name)
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


def combine_runtime_artifacts(
    p210: BootArtifacts,
    pluto: BootArtifacts,
    destination: Union[str, os.PathLike[str]],
    *,
    p210_kernel_name: str,
    provenance: Optional[Mapping[str, str]] = None,
) -> RuntimeCandidate:
    """Combine extracted inputs after auditing every ARM userspace contact."""

    if p210.kind != "p210-sd-boot" or p210.ramdisk is not None:
        raise FirmwareFormatError("P210 input must be the public kernel-only SD bundle")
    if pluto.ramdisk is None:
        raise FirmwareFormatError("Pluto input has no initramfs")
    rootfs = audit_pluto_rootfs(pluto.ramdisk)
    kernel_data = Path(p210.kernel).read_bytes()
    kernel = audit_p210_kernel_payload(kernel_data, p210_kernel_name)
    kernel_version = kernel.version
    abi_compatible = kernel_version >= rootfs.linux_abi_floor
    if not abi_compatible:
        raise FirmwareFormatError(
            "P210 kernel %s is older than rootfs ABI floor %s"
            % (".".join(map(str, kernel_version)), ".".join(map(str, rootfs.linux_abi_floor)))
        )

    output = Path(destination)
    dtb_data = Path(p210.devicetree).read_bytes()
    devicetree = audit_p210_runtime_dtb(dtb_data)
    ramdisk_data = Path(pluto.ramdisk).read_bytes()
    kernel_path = _atomic_copy(output / "p210-kernel.bin", kernel_data)
    dtb_path = _atomic_copy(output / "p210-devicetree.dtb", dtb_data)
    ramdisk_path = _atomic_copy(output / "pluto-v0.39-rootfs.cpio.gz", ramdisk_data)
    bootargs = (
        "console=ttyPS0,115200 earlycon=cdns,mmio,0xe0001000,115200n8 "
        "rdinit=/init rw loglevel=7"
    )
    artifacts = BootArtifacts(
        source=p210.source,
        kind="p210-public-kernel+official-pluto-v0.39-rootfs-abi-candidate",
        kernel=kernel_path,
        devicetree=dtb_path,
        ramdisk=ramdisk_path,
        bootargs=bootargs,
        configuration=pluto.configuration,
        hashes={
            "kernel": sha256_bytes(kernel_data),
            "devicetree": sha256_bytes(dtb_data),
            "ramdisk": sha256_bytes(ramdisk_data),
        },
        non_emulated_components=(
            "BOOT.BIN (P210 FSBL/U-Boot/FPGA payload)",
            "AD9361 and AXI DMA programmable-logic devices",
            "USB device controller FunctionFS endpoints",
        ),
    )
    limitations = [
        "The public P210 kernel and ADI v0.39 rootfs were not published as one vendor build.",
        "Stock QEMU xilinx-zynq-a9 does not execute the P210 FPGA bitstream or emulate AD9361/AXI-DMAC; the repository's P210-enabled machine supplies functional device models instead.",
        "The pinned community P210 XSA recipe states that its AD9361 is non-functional.",
    ]
    if not devicetree.hardware_usb_gadget_possible:
        limitations.append(
            "The public P210 DT configures its only enabled USB controller as host, while the rootfs expects a gadget UDC."
        )
    if not devicetree.qemu_gem_phy_matches:
        limitations.append(
            "The P210 DT PHY address does not match stock QEMU's fixed Zynq GEM PHY address 7."
        )
    return RuntimeCandidate(
        artifacts=artifacts,
        kernel=kernel,
        devicetree=devicetree,
        rootfs=rootfs,
        classification="ABI-compatible experimental composition; not vendor P210 firmware",
        kernel_name=p210_kernel_name,
        abi_compatible=True,
        provenance=dict(provenance or {}),
        limitations=tuple(limitations),
    )


def prepare_p210_runtime(
    p210_source: Union[str, os.PathLike[str]],
    pluto_source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
    *,
    provenance: Optional[Mapping[str, str]] = None,
) -> RuntimeCandidate:
    """Extract, ABI-audit, and assemble the public P210/official ADI candidate."""

    output = Path(destination)
    p210 = extract_p210_bundle(p210_source, output / ".inputs" / "p210")
    pluto = extract_pluto_archive(pluto_source, output / ".inputs" / "pluto")
    bundle = FirmwareBundle(Path(p210_source))
    try:
        kernel_name = UImage(bundle.files["uImage"]).header.name
    except KeyError as exc:
        raise FirmwareFormatError("P210 bundle lacks uImage") from exc
    return combine_runtime_artifacts(
        p210,
        pluto,
        output,
        p210_kernel_name=kernel_name,
        provenance=provenance,
    )


PROBE_MARKER_INIT = b"""#!/bin/sh
/bin/mount -t devtmpfs devtmpfs /dev
if (exec 0</dev/console) 2>/dev/null; then
    exec 0</dev/console
    exec 1>/dev/console
    exec 2>/dev/console
fi
echo 'NEPTUNE_RUNTIME userspace-init=ok'
/usr/sbin/iiod -V
status=$?
echo "NEPTUNE_RUNTIME iiod-exec=$status"
exec /sbin/init "$@"
"""


QEMU_TCP_IIOD_SERVICE = b"""#!/bin/sh
case "$1" in
  start)
    echo 'NEPTUNE_RUNTIME tcp-iiod-start=30431'
    start-stop-daemon -S -b -q -m -p /var/run/iiod-network.pid \\
      -x /usr/sbin/iiod -- -D -p 30431
    ;;
  stop)
    start-stop-daemon -K -q -p /var/run/iiod-network.pid
    ;;
  restart|reload)
    "$0" stop
    "$0" start
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|reload}" >&2
    exit 1
    ;;
esac
"""


QEMU_TCP_PROBE_INIT = b"""#!/bin/sh
/bin/mount -t devtmpfs devtmpfs /dev
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
if (exec 0</dev/console) 2>/dev/null; then
    exec 0</dev/console
    exec 1>/dev/console
    exec 2>/dev/console
fi
/sbin/ifconfig lo 127.0.0.1 up
/sbin/ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up
/sbin/route add default gw 10.0.2.2
echo 'NEPTUNE_RUNTIME network=10.0.2.15'
echo 'NEPTUNE_RUNTIME tcp-iiod-exec=30431'
exec /usr/sbin/iiod -D -p 30431
"""


QEMU_FFT_RUNTIME_INIT = b"""#!/bin/sh
/bin/mount -t devtmpfs devtmpfs /dev
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
if (exec 0</dev/console) 2>/dev/null; then
    exec 0</dev/console
    exec 1>/dev/console
    exec 2>/dev/console
fi
/sbin/ifconfig lo 127.0.0.1 up
/sbin/ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up
/sbin/route add default gw 10.0.2.2
echo -n 'NEPTUNE_RUNTIME cpu-online='
/bin/cat /sys/devices/system/cpu/online
echo 'NEPTUNE_RUNTIME network=10.0.2.15'
echo 'NEPTUNE_RUNTIME tcp-iiod-start=30431'
/usr/sbin/iiod -D -p 30431 &
echo 'NEPTUNE_RUNTIME fft-streamer-exec=30432'
exec /usr/bin/neptune-fft-streamer
"""


def build_iiod_probe_rootfs(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
) -> Path:
    """Build a deterministic test-only initramfs that executes real ``iiod -V``.

    Only ``/init`` is changed.  The production rootfs remains separately
    hashed, and the probe image is never represented as released firmware.
    """

    source_path = Path(source)
    archive = NewcArchive.from_gzip(source_path.read_bytes())
    current_mode = archive.entry("init").mode
    patched = archive.replaced("init", PROBE_MARKER_INIT, mode=current_mode | 0o111)
    return _atomic_copy(Path(destination), patched.to_gzip())


def build_qemu_tcp_rootfs(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
) -> Path:
    """Create a test-only image with TCP iiod independent of USB FunctionFS.

    The released S23 service initializes FunctionFS before entering the TCP
    server.  QEMU's Zynq machine has no P210 USB gadget implementation, so a
    separate S24 service starts the same released binary in network-only mode.
    No production file is removed or modified.
    """

    archive = NewcArchive.from_gzip(Path(source).read_bytes())
    inode = max((entry.inode for entry in archive.entries), default=0) + 1
    service = CpioEntry(
        name="etc/init.d/S24iiod-network-qemu",
        inode=inode,
        mode=0o100755,
        uid=0,
        gid=0,
        nlink=1,
        mtime=0,
        data=QEMU_TCP_IIOD_SERVICE,
    )
    patched = archive.added(service)
    return _atomic_copy(Path(destination), patched.to_gzip())


def build_qemu_tcp_probe_rootfs(
    source: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
) -> Path:
    """Create a fast test image that runs the released ARM ``iiod`` as PID 1.

    This intentionally bypasses the released flash and USB initialization
    scripts, both of which depend on devices absent from the virtual machine.
    It mounts the real kernel's ``proc``, ``sysfs`` and ``devtmpfs`` filesystems,
    assigns QEMU user-networking's conventional guest address, and then
    replaces itself with the unmodified released daemon.  The source release
    image remains byte-for-byte separate and content-addressed.
    """

    archive = NewcArchive.from_gzip(Path(source).read_bytes())
    current_mode = archive.entry("init").mode
    patched = archive.replaced(
        "init",
        QEMU_TCP_PROBE_INIT,
        mode=current_mode | 0o111,
    )
    return _atomic_copy(Path(destination), patched.to_gzip())


def build_qemu_fft_runtime_rootfs(
    source: Union[str, os.PathLike[str]],
    streamer: Union[str, os.PathLike[str]],
    destination: Union[str, os.PathLike[str]],
) -> Path:
    """Add the static ARM FFT streamer and make it the hardware-test service.

    The released rootfs remains a separate content-addressed input.  This
    derived image runs the released ``iiod`` for control-plane access and the
    explicitly supplied ARM EABI5 static executable for the IIO-DMAC -> Linux
    IIO block -> CPU copy -> PL FFT DMA -> NSFT/TCP data plane. The launch
    command reserves the streamer's fixed windows by limiting Linux to 384 MiB
    of the emulated 512 MiB DDR. Those ``/dev/mem`` windows are a QEMU harness
    mechanism, not a physical-board DMA/cache-coherency design.
    """

    streamer_path = Path(streamer)
    executable = streamer_path.read_bytes()
    elf = ELF32ARM(executable)
    if elf.eabi_version != 5 or elf.float_abi != "hard":
        raise FirmwareFormatError("FFT streamer is not ARM EABI5 hard-float")
    if elf.interpreter is not None or elf.needed:
        raise FirmwareFormatError("FFT streamer must be statically linked")

    archive = NewcArchive.from_gzip(Path(source).read_bytes())
    if archive.contains("usr/bin/neptune-fft-streamer"):
        raise FirmwareFormatError("rootfs already contains an FFT streamer")
    inode = max((entry.inode for entry in archive.entries), default=0) + 1
    program = CpioEntry(
        name="usr/bin/neptune-fft-streamer",
        inode=inode,
        mode=0o100755,
        uid=0,
        gid=0,
        nlink=1,
        mtime=0,
        data=executable,
    )
    current_mode = archive.entry("init").mode
    patched = archive.added(program).replaced(
        "init",
        QEMU_FFT_RUNTIME_INIT,
        mode=current_mode | 0o111,
    )
    return _atomic_copy(Path(destination), patched.to_gzip())


__all__ = [
    "CpioEntry",
    "ELF32ARM",
    "DeviceTreeRuntimeAudit",
    "KernelAudit",
    "NewcArchive",
    "P210_REQUIRED_KERNEL_CONFIG",
    "PROBE_MARKER_INIT",
    "QEMU_TCP_IIOD_SERVICE",
    "QEMU_TCP_PROBE_INIT",
    "QEMU_FFT_RUNTIME_INIT",
    "RootfsAudit",
    "RuntimeCandidate",
    "audit_pluto_rootfs",
    "audit_p210_kernel_payload",
    "audit_p210_runtime_dtb",
    "build_iiod_probe_rootfs",
    "build_qemu_tcp_rootfs",
    "build_qemu_tcp_probe_rootfs",
    "build_qemu_fft_runtime_rootfs",
    "combine_runtime_artifacts",
    "prepare_p210_runtime",
]
