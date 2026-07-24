#!/usr/bin/env python3
"""Drive the P210 operator device inside QEMU via qtest and verify the block
output is bit-exact to the golden full-operator vector.

No guest boot: the qtest accelerator lets this process write guest DRAM and the
operator's MMIO registers directly, exercising the exact register/DMA contract
the firmware would. This is the operator actually executing in the twin.

Usage: run_operator_qtest.py <qemu-system-arm> <twiddle-rom-q117.bin>
Exit 0 iff the emulated operator's output matches the golden pin.
"""
import hashlib
import struct
import subprocess
import sys

OP_BASE = 0x7C450000
INPUT_ADDR = 0x18000000
OUTPUT2_ADDR = 0x18180000
WEIGHT_ADDR = 0x18200000
N = 256
LOG2N = 8
B_Q23 = -300000 & 0xFFFFFFFF
GOLDEN_PIN = "2b994fa7094492fb9bdd120708b512a835f039a424dc502fd61991fdb9c0901d"

# register offsets
CONTROL, STATUS = 0x00C, 0x010
LOG2_N, INPUT, RESULT_SEQ = 0x018, 0x024, 0x038
OP_MODE_COUNT, OP_OUTPUT_MODE, OP_FLAGS = 0x084, 0x088, 0x08C
WEIGHT_A, WEIGHT_B, WEIGHT_CRC, ACTIVE_CRC, RESULT_CRC = 0x090, 0x094, 0x0A0, 0x0A4, 0x0AC
OUTPUT2, THRESHOLD = 0x0DC, 0x0E4
CTRL_ACTIVATE, CTRL_START = 0x400, 0x001
ST_DONE, ST_WT_READY, ST_BANK_CRC_FAIL = 0x02, 0x10, 0x20


def splitmix64(seed, count):
    out = []
    x = seed & 0xFFFFFFFFFFFFFFFF
    for _ in range(count):
        x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z = z ^ (z >> 31)
        out.append(z)
    return out


def golden_input(seed, n):
    words = splitmix64(seed, n)
    b = bytearray()
    for w in words:
        r = w & 0xFFFF
        i = (w >> 16) & 0xFFFF
        r = r - 65536 if r >= 32768 else r
        i = i - 65536 if i >= 32768 else i
        b += struct.pack("<ii", r << 8, i << 8)
    return bytes(b)


def crc32(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


class QTest:
    def __init__(self, qemu):
        self.p = subprocess.Popen(
            [qemu, "-machine", "xilinx-zynq-a9,p210=on,p210-operator=on", "-accel", "qtest",
             "-m", "1024", "-display", "none", "-qtest", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)

    def cmd(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline().strip()
        if not line.startswith("OK"):
            raise RuntimeError(f"qtest {s!r} -> {line!r}")
        return line

    def writel(self, addr, val):
        self.cmd(f"writel 0x{addr:x} 0x{val & 0xFFFFFFFF:x}")

    def readl(self, addr):
        return int(self.cmd(f"readl 0x{addr:x}").split()[1], 16)

    def write_mem(self, addr, data):
        # qtest 'write ADDR SIZE 0xHEX' in chunks
        step = 512
        for off in range(0, len(data), step):
            chunk = data[off:off + step]
            self.cmd(f"write 0x{addr+off:x} 0x{len(chunk):x} 0x{chunk.hex()}")

    def read_mem(self, addr, size):
        out = bytearray()
        step = 512
        for off in range(0, size, step):
            n = min(step, size - off)
            hexs = self.cmd(f"read 0x{addr+off:x} 0x{n:x}").split()[1][2:]
            out += bytes.fromhex(hexs)
        return bytes(out)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    qemu, _rom = sys.argv[1], sys.argv[2]
    q = QTest(qemu)
    fails = []
    try:
        # 1. input into DRAM
        q.write_mem(INPUT_ADDR, golden_input(31, N))
        # 2. weight bank: [modes u32][hr int16*N][hi int16*N], H = flat 0.5
        blob = struct.pack("<I", N) + b"".join(struct.pack("<h", 1 << 14) for _ in range(N)) \
               + b"".join(struct.pack("<h", 0) for _ in range(N))
        q.write_mem(WEIGHT_ADDR, blob)
        wcrc = crc32(blob)
        # 3. configure registers
        q.writel(OP_BASE + LOG2_N, LOG2N)
        q.writel(OP_BASE + INPUT, INPUT_ADDR)
        q.writel(OP_BASE + OUTPUT2, OUTPUT2_ADDR)
        q.writel(OP_BASE + OP_MODE_COUNT, N)
        q.writel(OP_BASE + OP_OUTPUT_MODE, 1)
        q.writel(OP_BASE + THRESHOLD, B_Q23)
        q.writel(OP_BASE + WEIGHT_A, WEIGHT_ADDR)
        q.writel(OP_BASE + WEIGHT_B, len(blob))
        q.writel(OP_BASE + WEIGHT_CRC, wcrc)
        q.writel(OP_BASE + OP_FLAGS, 0)          # clear bypass
        # 4. activate + start through CONTROL (the ABI transaction)
        q.writel(OP_BASE + CONTROL, CTRL_ACTIVATE)
        if not (q.readl(OP_BASE + STATUS) & ST_WT_READY):
            fails.append("ACTIVATE: weight not ready")
        q.writel(OP_BASE + CONTROL, CTRL_START)
        if not (q.readl(OP_BASE + STATUS) & ST_DONE):
            fails.append("block did not complete")
        # 5. read output, digest, compare to golden
        out = q.read_mem(OUTPUT2_ADDR, N * 8)
        re = b"".join(out[8 * i:8 * i + 4] for i in range(N))
        im = b"".join(out[8 * i + 4:8 * i + 8] for i in range(N))
        digest = hashlib.sha256(re + im).hexdigest()
        if digest == GOLDEN_PIN:
            print("operator block in QEMU: MATCH golden")
        else:
            fails.append(f"output diverged: got {digest} want {GOLDEN_PIN}")
        # 6. result-weight attribution latched
        if q.readl(OP_BASE + RESULT_CRC) != wcrc:
            fails.append("RESULT_WEIGHT_CRC not latched")
        if q.readl(OP_BASE + RESULT_SEQ) != 1:
            fails.append("RESULT_SEQUENCE not advanced")
        # 7. CRC gate: corrupt the blob, re-activate, expect BANK_CRC_FAIL
        q.writel(OP_BASE + CONTROL, 0x2)         # soft reset
        bad = bytearray(blob); bad[8] ^= 0xFF
        q.write_mem(WEIGHT_ADDR, bytes(bad))
        q.writel(OP_BASE + WEIGHT_A, WEIGHT_ADDR)
        q.writel(OP_BASE + WEIGHT_B, len(blob))
        q.writel(OP_BASE + WEIGHT_CRC, wcrc)     # stale crc
        q.writel(OP_BASE + CONTROL, CTRL_ACTIVATE)
        if not (q.readl(OP_BASE + STATUS) & ST_BANK_CRC_FAIL):
            fails.append("CRC gate did not reject corrupted bank")
        else:
            print("CRC gate in QEMU: corrupted bank rejected (BANK_INTEGRITY)")
    finally:
        q.close()

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("P210_OPERATOR_QEMU PASS (operator executes in the twin through the v2 ABI, bit-exact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
