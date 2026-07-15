"""Zynq-7020 processing-system, sparse memory, and ADI AXI fabric model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import struct
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .ad9361 import AD9361
from .clock import ScheduledHandle, VirtualClock
from .errors import InvalidTransition, OutOfRange
from .trace import TraceLog


class BootSource(str, Enum):
    QSPI = "qspi"
    SD = "sd"
    JTAG = "jtag"


class BootStage(str, Enum):
    OFF = "off"
    BOOT_ROM = "boot_rom"
    FSBL = "fsbl"
    U_BOOT = "u_boot"
    KERNEL = "kernel"
    RUNNING = "running"
    FAILED = "failed"


class SparseMemory:
    """Byte-addressable, zero-filled memory without allocating the full DDR."""

    PAGE_SIZE = 4096

    def __init__(self, size: int, readonly: bool = False) -> None:
        if size <= 0:
            raise ValueError("memory size must be positive")
        self.size = int(size)
        self.readonly = readonly
        self._pages: Dict[int, bytearray] = {}

    def read(self, offset: int, length: int) -> bytes:
        self._check(offset, length)
        result = bytearray(length)
        cursor = 0
        while cursor < length:
            absolute = offset + cursor
            page_number, page_offset = divmod(absolute, self.PAGE_SIZE)
            count = min(length - cursor, self.PAGE_SIZE - page_offset)
            page = self._pages.get(page_number)
            if page is not None:
                result[cursor : cursor + count] = page[page_offset : page_offset + count]
            cursor += count
        return bytes(result)

    def write(self, offset: int, data: bytes) -> None:
        data = bytes(data)
        self._check(offset, len(data))
        if self.readonly:
            raise PermissionError("memory region is read-only")
        cursor = 0
        while cursor < len(data):
            absolute = offset + cursor
            page_number, page_offset = divmod(absolute, self.PAGE_SIZE)
            count = min(len(data) - cursor, self.PAGE_SIZE - page_offset)
            page = self._pages.setdefault(page_number, bytearray(self.PAGE_SIZE))
            page[page_offset : page_offset + count] = data[cursor : cursor + count]
            if not any(page):
                del self._pages[page_number]
            cursor += count

    @property
    def resident_pages(self) -> int:
        return len(self._pages)

    def sha256(self) -> str:
        digest = hashlib.sha256()
        for number, page in sorted(self._pages.items()):
            digest.update(struct.pack(">Q", number))
            digest.update(page)
        return digest.hexdigest()

    def _check(self, offset: int, length: int) -> None:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise OutOfRange("memory access [%#x, %#x) exceeds region" % (offset, offset + length))


class MMIODevice:
    """Little-endian register bank with optional 32-bit side effects."""

    def __init__(self, size: int) -> None:
        self.size = int(size)
        self._registers = bytearray(size)

    def read(self, offset: int, length: int) -> bytes:
        self._check(offset, length)
        if length == 4 and offset % 4 == 0:
            return struct.pack("<I", self.read32(offset))
        return bytes(self._registers[offset : offset + length])

    def write(self, offset: int, data: bytes) -> None:
        data = bytes(data)
        self._check(offset, len(data))
        if len(data) == 4 and offset % 4 == 0:
            self.write32(offset, struct.unpack("<I", data)[0])
            return
        self._registers[offset : offset + len(data)] = data

    def read32(self, offset: int) -> int:
        self._check(offset, 4)
        return struct.unpack_from("<I", self._registers, offset)[0]

    def write32(self, offset: int, value: int) -> None:
        self._check(offset, 4)
        struct.pack_into("<I", self._registers, offset, int(value) & 0xFFFFFFFF)

    def _check(self, offset: int, length: int) -> None:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise OutOfRange("MMIO access exceeds register bank")


class ZynqUART(MMIODevice):
    CONTROL = 0x00
    MODE = 0x04
    CHANNEL_STATUS = 0x2C
    FIFO = 0x30
    STATUS_TXEMPTY = 1 << 3
    STATUS_TACTIVE = 1 << 11

    def __init__(self) -> None:
        super().__init__(0x1000)
        self.tx = bytearray()
        self.rx = bytearray()

    def read32(self, offset: int) -> int:
        if offset == self.CHANNEL_STATUS:
            value = self.STATUS_TXEMPTY
            if self.rx:
                value |= 1 << 1  # RX trigger/non-empty approximation.
            return value
        if offset == self.FIFO:
            return self.rx.pop(0) if self.rx else 0
        return super().read32(offset)

    def write32(self, offset: int, value: int) -> None:
        if offset == self.FIFO:
            self.tx.append(value & 0xFF)
            return
        super().write32(offset, value)

    def inject_rx(self, data: bytes) -> None:
        self.rx.extend(data)

    def drain_tx(self) -> bytes:
        data = bytes(self.tx)
        self.tx.clear()
        return data


class ZynqSPI(MMIODevice):
    """Zynq SPI controller contact connected to the AD9361 register port."""

    def __init__(self, radio: AD9361) -> None:
        super().__init__(0x1000)
        self.radio = radio

    def transfer(self, transaction: bytes) -> bytes:
        return self.radio.spi_transfer(transaction)


class ADIAxiCore(MMIODevice):
    REG_VERSION = 0x0000
    REG_ID = 0x0004
    REG_SCRATCH = 0x0008

    def __init__(self, size: int, version: int, core_id: int) -> None:
        super().__init__(size)
        self.write32(self.REG_VERSION, version)
        self.write32(self.REG_ID, core_id)


@dataclass(frozen=True)
class _DMADescriptor:
    transfer_id: int
    flags: int
    address: int
    length: int


class AXIDMAC(MMIODevice):
    """P210 AXI-DMAC 4.00.a contact with deterministic timed completion."""

    REG_VERSION = 0x000
    REG_ID = 0x004
    REG_IRQ_MASK = 0x080
    REG_IRQ_PENDING = 0x084
    REG_IRQ_SOURCE = 0x088
    REG_CONTROL = 0x400
    REG_TRANSFER_ID = 0x404
    REG_START_TRANSFER = 0x408
    REG_FLAGS = 0x40C
    REG_DEST_ADDRESS = 0x410
    REG_SRC_ADDRESS = 0x414
    REG_X_LENGTH = 0x418
    REG_Y_LENGTH = 0x41C
    REG_DEST_STRIDE = 0x420
    REG_SRC_STRIDE = 0x424
    REG_TRANSFER_DONE = 0x428
    REG_ACTIVE_TRANSFER_ID = 0x42C
    REG_STATUS = 0x430
    REG_CURRENT_DEST_ADDRESS = 0x434
    REG_CURRENT_SRC_ADDRESS = 0x438

    VERSION = 0x00040061
    IRQ_TRANSFER_QUEUED = 1 << 0
    IRQ_TRANSFER_COMPLETED = 1 << 1
    IRQ_MASK_ALL = IRQ_TRANSFER_QUEUED | IRQ_TRANSFER_COMPLETED
    CONTROL_ENABLE = 1 << 0
    CONTROL_PAUSE = 1 << 1
    CONTROL_MASK = CONTROL_ENABLE | CONTROL_PAUSE
    FLAG_CYCLIC = 1 << 0
    FLAG_TLAST = 1 << 1
    DMA_LENGTH_WIDTH = 24
    X_LENGTH_MASK = 0x00FFFFFF
    MEMORY_ADDRESS_WIDTH = 29
    MEMORY_BEAT_BYTES = 8
    ADDRESS_MASK = 0x1FFFFFF8
    SUPPORTS_2D = False
    QUEUE_DEPTH = 4

    def __init__(
        self,
        clock: VirtualClock,
        direction: str,
        bytes_per_second: int = 491_520_000,
        on_transfer: Optional[Callable[[int, int, int], None]] = None,
    ) -> None:
        super().__init__(0x10000)
        if direction not in {"device_to_memory", "memory_to_device"}:
            raise ValueError("invalid DMA direction")
        self.clock = clock
        self.direction = direction
        self.bytes_per_second = int(bytes_per_second)
        if self.bytes_per_second <= 0:
            raise ValueError("bytes_per_second must be positive")
        self.on_transfer = on_transfer
        self.supports_cyclic = direction == "memory_to_device"
        self._queue: List[_DMADescriptor] = []
        self._timer: Optional[ScheduledHandle] = None
        self._active_deadline_ns: Optional[int] = None
        self._pause_remaining_ns = 0
        self.busy = False
        self.completed_transfer_id = -1
        self.reset()

    @property
    def queued_transfers(self) -> int:
        return len(self._queue)

    def reset(self) -> None:
        self._cancel_timer()
        self._registers[:] = bytes(self.size)
        self._queue = []
        self._pause_remaining_ns = 0
        self.busy = False
        self.completed_transfer_id = -1
        super().write32(self.REG_VERSION, self.VERSION)
        super().write32(self.REG_ID, 0)
        super().write32(self.REG_IRQ_MASK, self.IRQ_MASK_ALL)
        super().write32(
            self.REG_FLAGS,
            self.FLAG_TLAST | (self.FLAG_CYCLIC if self.supports_cyclic else 0),
        )

    def write32(self, offset: int, value: int) -> None:
        value = int(value) & 0xFFFFFFFF

        if offset == self.REG_IRQ_PENDING:
            source = super().read32(self.REG_IRQ_SOURCE)
            super().write32(
                self.REG_IRQ_SOURCE, source & ~(value & self.IRQ_MASK_ALL)
            )
            self._update_irq_pending()
            return
        if offset in {
            self.REG_VERSION,
            self.REG_ID,
            self.REG_IRQ_SOURCE,
            self.REG_TRANSFER_ID,
            self.REG_TRANSFER_DONE,
            self.REG_ACTIVE_TRANSFER_ID,
            self.REG_STATUS,
            self.REG_CURRENT_DEST_ADDRESS,
            self.REG_CURRENT_SRC_ADDRESS,
        }:
            return
        if offset == self.REG_IRQ_MASK:
            super().write32(offset, value & self.IRQ_MASK_ALL)
            self._update_irq_pending()
            return
        if offset == self.REG_CONTROL:
            self._write_control(value)
            return
        if offset == self.REG_START_TRANSFER:
            if value & 1 and super().read32(offset) == 0:
                if not super().read32(self.REG_CONTROL) & self.CONTROL_ENABLE:
                    return
                super().write32(offset, 1)
                self._accept_descriptor()
            return
        if offset == self.REG_X_LENGTH:
            super().write32(offset, value & self.X_LENGTH_MASK)
            return
        if offset in {
            self.REG_Y_LENGTH,
            self.REG_DEST_STRIDE,
            self.REG_SRC_STRIDE,
        }:
            super().write32(offset, 0)
            return
        if offset == self.REG_DEST_ADDRESS:
            super().write32(
                offset,
                value & self.ADDRESS_MASK
                if self.direction == "device_to_memory"
                else 0,
            )
            return
        if offset == self.REG_SRC_ADDRESS:
            super().write32(
                offset,
                value & self.ADDRESS_MASK
                if self.direction == "memory_to_device"
                else 0,
            )
            return
        if offset == self.REG_FLAGS:
            allowed = self.FLAG_TLAST
            if self.supports_cyclic:
                allowed |= self.FLAG_CYCLIC
            super().write32(offset, value & allowed)
            return
        super().write32(offset, value)

    def _write_control(self, value: int) -> None:
        old = super().read32(self.REG_CONTROL)
        control = value & self.CONTROL_MASK
        was_paused = bool(old & self.CONTROL_PAUSE)
        paused = bool(control & self.CONTROL_PAUSE)
        super().write32(self.REG_CONTROL, control)

        if not control & self.CONTROL_ENABLE:
            self._cancel_timer()
            self._queue = []
            self._pause_remaining_ns = 0
            self.busy = False
            self.completed_transfer_id = -1
            for register in (
                self.REG_TRANSFER_ID,
                self.REG_START_TRANSFER,
                self.REG_TRANSFER_DONE,
                self.REG_ACTIVE_TRANSFER_ID,
                self.REG_STATUS,
            ):
                super().write32(register, 0)
            return

        if not was_paused and paused and self.busy:
            deadline = self._active_deadline_ns
            self._pause_remaining_ns = max(
                1, (deadline - self.clock.now_ns) if deadline is not None else 1
            )
            self._cancel_timer()
        elif was_paused and not paused and self.busy:
            delay_ns = self._pause_remaining_ns or self._descriptor_delay(
                self._queue[0]
            )
            self._pause_remaining_ns = 0
            self._arm_timer(delay_ns)

    def _update_irq_pending(self) -> None:
        source = super().read32(self.REG_IRQ_SOURCE) & self.IRQ_MASK_ALL
        mask = super().read32(self.REG_IRQ_MASK) & self.IRQ_MASK_ALL
        super().write32(self.REG_IRQ_PENDING, source & ~mask)

    def _raise_irq_source(self, bits: int) -> None:
        source = super().read32(self.REG_IRQ_SOURCE)
        super().write32(self.REG_IRQ_SOURCE, source | (bits & self.IRQ_MASK_ALL))
        self._update_irq_pending()

    def _accept_descriptor(self) -> bool:
        if len(self._queue) == self.QUEUE_DEPTH:
            return False

        transfer_id = super().read32(self.REG_TRANSFER_ID) & (
            self.QUEUE_DEPTH - 1
        )
        flags = super().read32(self.REG_FLAGS)
        descriptor = _DMADescriptor(
            transfer_id=transfer_id,
            flags=flags,
            address=super().read32(
                self.REG_DEST_ADDRESS
                if self.direction == "device_to_memory"
                else self.REG_SRC_ADDRESS
            ),
            length=super().read32(self.REG_X_LENGTH) + 1,
        )
        self._queue.append(descriptor)
        super().write32(self.REG_START_TRANSFER, 0)

        if not descriptor.flags & self.FLAG_CYCLIC:
            done = super().read32(self.REG_TRANSFER_DONE)
            super().write32(
                self.REG_TRANSFER_DONE, done & ~(1 << transfer_id)
            )
            super().write32(
                self.REG_TRANSFER_ID,
                (transfer_id + 1) & (self.QUEUE_DEPTH - 1),
            )
            self._raise_irq_source(self.IRQ_TRANSFER_QUEUED)
        self._schedule_head()
        return True

    def _schedule_head(self) -> None:
        if self.busy or not self._queue:
            return
        descriptor = self._queue[0]
        self.busy = True
        super().write32(self.REG_ACTIVE_TRANSFER_ID, descriptor.transfer_id)
        delay_ns = self._descriptor_delay(descriptor)
        if super().read32(self.REG_CONTROL) & self.CONTROL_PAUSE:
            self._pause_remaining_ns = delay_ns
        else:
            self._arm_timer(delay_ns)

    def _descriptor_delay(self, descriptor: _DMADescriptor) -> int:
        return max(
            1,
            (
                descriptor.length * 1_000_000_000
                + self.bytes_per_second
                - 1
            )
            // self.bytes_per_second,
        )

    def _arm_timer(self, delay_ns: int) -> None:
        self._active_deadline_ns = self.clock.now_ns + delay_ns
        self._timer = self.clock.schedule(
            delay_ns,
            self._complete_head,
            "axi-dmac-%s" % self.direction,
        )

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._active_deadline_ns = None

    def _complete_head(self) -> None:
        self._timer = None
        self._active_deadline_ns = None
        if not self.busy or not self._queue:
            return

        descriptor = self._queue[0]
        current_address_register = (
            self.REG_CURRENT_DEST_ADDRESS
            if self.direction == "device_to_memory"
            else self.REG_CURRENT_SRC_ADDRESS
        )
        super().write32(
            current_address_register, descriptor.address + descriptor.length
        )

        if descriptor.flags & self.FLAG_CYCLIC:
            if self.on_transfer is not None:
                self.on_transfer(
                    descriptor.address,
                    descriptor.length,
                    descriptor.transfer_id,
                )
            if self.busy and self._queue and self._queue[0] == descriptor:
                if super().read32(self.REG_CONTROL) & self.CONTROL_PAUSE:
                    self._pause_remaining_ns = self._descriptor_delay(descriptor)
                else:
                    self._arm_timer(self._descriptor_delay(descriptor))
            return

        done = super().read32(self.REG_TRANSFER_DONE)
        super().write32(
            self.REG_TRANSFER_DONE, done | (1 << descriptor.transfer_id)
        )
        self.completed_transfer_id = descriptor.transfer_id
        self._raise_irq_source(self.IRQ_TRANSFER_COMPLETED)
        self._queue.pop(0)
        self.busy = False
        if self._queue:
            self._schedule_head()
        else:
            super().write32(
                self.REG_ACTIVE_TRANSFER_ID,
                super().read32(self.REG_TRANSFER_ID),
            )

        if super().read32(self.REG_START_TRANSFER) & 1:
            self._accept_descriptor()

        if self.on_transfer is not None:
            self.on_transfer(
                descriptor.address,
                descriptor.length,
                descriptor.transfer_id,
            )


@dataclass(frozen=True)
class AddressRegion:
    name: str
    start: int
    size: int
    device: object
    observed_extent: Optional[int] = None

    @property
    def end(self) -> int:
        return self.start + self.size

    def contains(self, address: int, length: int) -> bool:
        return self.start <= address and address + length <= self.end


class MemoryMap:
    def __init__(self) -> None:
        self._regions: List[AddressRegion] = []

    @property
    def regions(self) -> Tuple[AddressRegion, ...]:
        return tuple(self._regions)

    def add(self, region: AddressRegion) -> None:
        if region.size <= 0:
            raise ValueError("region size must be positive")
        for existing in self._regions:
            if region.start < existing.end and existing.start < region.end:
                raise ValueError(
                    "address region %s overlaps %s" % (region.name, existing.name)
                )
        self._regions.append(region)
        self._regions.sort(key=lambda item: item.start)

    def locate(self, address: int, length: int = 1) -> Tuple[AddressRegion, int]:
        if length < 0:
            raise ValueError("length must be non-negative")
        for region in self._regions:
            if region.contains(address, length):
                return region, address - region.start
        raise OutOfRange("unmapped physical address [%#x, %#x)" % (address, address + length))

    def read(self, address: int, length: int) -> bytes:
        region, offset = self.locate(address, length)
        return region.device.read(offset, length)

    def write(self, address: int, data: bytes) -> None:
        region, offset = self.locate(address, len(data))
        region.device.write(offset, data)


class Zynq7020:
    """P210-specific Zynq PS/PL composition at observed physical addresses."""

    DDR_BYTES = 512 * 1024 * 1024
    CPU_CORES = 2
    # The storefront says 766 MHz, but the pinned public P210 XSA configures
    # PCW_ACT_APU_PERIPHERAL_FREQMHZ=666.666687.  The latter is the executable
    # hardware handoff and remains subject to delivered-unit confirmation.
    CPU_HZ = 666_666_687

    def __init__(
        self,
        radio: AD9361,
        clock: Optional[VirtualClock] = None,
        trace: Optional[TraceLog] = None,
    ) -> None:
        self.radio = radio
        self.clock = clock or radio.clock
        self.trace = trace or radio.trace
        self.map = MemoryMap()
        self.ddr = SparseMemory(self.DDR_BYTES)
        self.ocm = SparseMemory(256 * 1024)
        self.uart = ZynqUART()
        self.spi = ZynqSPI(radio)
        self.usb = MMIODevice(0x1000)
        self.gem = MMIODevice(0x1000)
        self.sdhci = MMIODevice(0x1000)
        self.gpio = MMIODevice(0x1000)
        self.qspi = MMIODevice(0x1000)
        self.slcr = MMIODevice(0x1000)
        self.rx_core = ADIAxiCore(0x4000, 0x00060000, 0x41444300)
        self.tx_core = ADIAxiCore(0x1000, 0x00060000, 0x44445300)
        self.rx_dma = AXIDMAC(self.clock, "device_to_memory")
        self.tx_dma = AXIDMAC(self.clock, "memory_to_device")
        self._install_map()
        self.boot_source: Optional[BootSource] = None
        self.boot_stage = BootStage.OFF
        self.boot_failure: Optional[str] = None
        self._boot_callbacks: List[ScheduledHandle] = []

    def _install_map(self) -> None:
        regions = [
            AddressRegion("ddr", 0x00000000, self.DDR_BYTES, self.ddr),
            AddressRegion("axi-ad9361-rx", 0x79020000, 0x4000, self.rx_core, 0x6000),
            AddressRegion("axi-ad9361-tx-dds", 0x79024000, 0x1000, self.tx_core),
            AddressRegion("axi-dmac-rx", 0x7C400000, 0x10000, self.rx_dma),
            AddressRegion("axi-dmac-tx", 0x7C420000, 0x10000, self.tx_dma),
            AddressRegion("uart1", 0xE0001000, 0x1000, self.uart),
            AddressRegion("usb0", 0xE0002000, 0x1000, self.usb),
            AddressRegion("spi0", 0xE0006000, 0x1000, self.spi),
            AddressRegion("gpio", 0xE000A000, 0x1000, self.gpio),
            AddressRegion("gem0", 0xE000B000, 0x1000, self.gem),
            AddressRegion("qspi", 0xE000D000, 0x1000, self.qspi),
            AddressRegion("sdhci0", 0xE0100000, 0x1000, self.sdhci),
            AddressRegion("slcr", 0xF8000000, 0x1000, self.slcr),
            AddressRegion("ocm", 0xFFFC0000, 0x40000, self.ocm),
        ]
        for region in regions:
            self.map.add(region)

    def read(self, address: int, length: int) -> bytes:
        return self.map.read(address, length)

    def write(self, address: int, data: bytes) -> None:
        self.map.write(address, data)

    def read32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def write32(self, address: int, value: int) -> None:
        self.write(address, struct.pack("<I", int(value) & 0xFFFFFFFF))

    def power_on(self, source: BootSource = BootSource.QSPI, kernel_available: bool = True) -> None:
        if self.boot_stage != BootStage.OFF:
            raise InvalidTransition("Zynq is already powered")
        self._cancel_boot_callbacks()
        self.boot_source = BootSource(source)
        self.boot_stage = BootStage.BOOT_ROM
        self.boot_failure = None
        self._boot_event("boot_rom")

        def enter_fsbl() -> None:
            self.boot_stage = BootStage.FSBL
            self._boot_event("fsbl")

        def enter_uboot() -> None:
            self.boot_stage = BootStage.U_BOOT
            self._boot_event("u_boot")

        def enter_kernel() -> None:
            if not kernel_available:
                self.boot_stage = BootStage.FAILED
                self.boot_failure = "kernel artifact unavailable or rejected"
                self._boot_event("failed")
                return
            self.boot_stage = BootStage.KERNEL
            self._boot_event("kernel")

        def enter_running() -> None:
            if self.boot_stage == BootStage.KERNEL:
                self.boot_stage = BootStage.RUNNING
                self._boot_event("running")

        self._boot_callbacks = [
            self.clock.schedule(1_000_000, enter_fsbl, "zynq-fsbl"),
            self.clock.schedule(6_000_000, enter_uboot, "zynq-uboot"),
            self.clock.schedule(16_000_000, enter_kernel, "zynq-kernel"),
            self.clock.schedule(116_000_000, enter_running, "zynq-userspace"),
        ]

    def power_off(self) -> None:
        self._cancel_boot_callbacks()
        self.rx_dma.reset()
        self.tx_dma.reset()
        self.boot_stage = BootStage.OFF
        self.boot_source = None
        self.boot_failure = None
        self._boot_event("power_off")

    def _cancel_boot_callbacks(self) -> None:
        for callback in self._boot_callbacks:
            callback.cancel()
        self._boot_callbacks = []

    def snapshot(self) -> Dict[str, object]:
        return {
            "boot_source": self.boot_source.value if self.boot_source else None,
            "boot_stage": self.boot_stage.value,
            "boot_failure": self.boot_failure,
            "cpu_cores": self.CPU_CORES,
            "cpu_hz": self.CPU_HZ,
            "ddr_bytes": self.DDR_BYTES,
            "ddr_resident_pages": self.ddr.resident_pages,
            "ddr_sparse_sha256": self.ddr.sha256(),
            "regions": [
                {
                    "name": region.name,
                    "start": region.start,
                    "size": region.size,
                    "observed_extent": region.observed_extent,
                }
                for region in self.map.regions
            ],
        }

    def _boot_event(self, event: str) -> None:
        self.trace.append(
            logical_ns=self.clock.now_ns,
            contact="zynq.boot",
            direction="internal",
            event=event,
            payload={
                "stage": self.boot_stage.value,
                "source": self.boot_source.value if self.boot_source else None,
            },
        )
