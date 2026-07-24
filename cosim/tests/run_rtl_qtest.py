#!/usr/bin/env python3
"""Drive the Verilated RTL block inside QEMU via qtest and verify its output is
bit-exact to the golden vector.

This is the custom-FPGA path end to end: QEMU's p210-rtl device dlopen()s the
Verilated Verilog block named by $P210_RTL_LIB and, on CONTROL.START, clocks the
real RTL over the machine's MMIO/DMA. No guest boot: qtest writes guest DRAM and
the device registers directly, exercising the exact register/DMA contract the
firmware uses. The datapath under test is the Verilog itself.

Usage: run_rtl_qtest.py <qemu-system-arm> <lib.so> <vectors-dir> [N]
  N defaults to 1024. vectors-dir holds in_re_<N>.memh in_im_<N>.memh
  exp_re_<N>.memh exp_im_<N>.memh. A ROM-less block needs no rom_cos.memh.
Exit 0 iff the emulated RTL's output matches the golden pin.
"""
import os
import struct
import subprocess
import sys

RTL_BASE = 0x7C450000
INPUT_ADDR = 0x18000000
OUTPUT_ADDR = 0x18180000

CONTROL, STATUS, ERROR_CODE = 0x00C, 0x010, 0x014
LOG2_N, INPUT, RESULT_SEQ, OUTPUT = 0x018, 0x024, 0x038, 0x0DC
CTRL_START, CTRL_SOFT_RESET = 0x001, 0x002
ST_DONE, ST_ERROR = 0x02, 0x04


def load_memh(path, n):
    """Parse an $readmemh file of 24-bit hex words; sign-extend to int32."""
    vals = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            v = int(line, 16) & 0xFFFFFF
            vals.append(v - (1 << 24) if v & 0x800000 else v)
            if len(vals) == n:
                break
    if len(vals) != n:
        raise RuntimeError(f"{path}: expected {n} words, got {len(vals)}")
    return vals


class QTest:
    def __init__(self, qemu, libpath):
        libpath = os.path.abspath(libpath)
        env = dict(os.environ, P210_RTL_LIB=libpath)
        # A block that uses $readmemh("foo.memh") resolves it relative to the
        # process cwd, so run QEMU one level above the library's obj_ dir (the
        # rtl-cosim dir), where such init files are kept. ROM-less blocks (no
        # $readmemh) need nothing there -- this is not an error.
        rom_dir = os.path.dirname(os.path.dirname(libpath))
        if not os.path.exists(os.path.join(rom_dir, "rom_cos.memh")):
            print(f"note: no rom_cos.memh in {rom_dir}; assuming a ROM-less block")
        self.p = subprocess.Popen(
            [qemu, "-machine", "xilinx-zynq-a9,p210=on,p210-rtl=on", "-accel", "qtest",
             "-m", "1024", "-display", "none", "-qtest", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd=rom_dir)

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
        step = 512
        for off in range(0, len(data), step):
            chunk = data[off:off + step]
            self.cmd(f"write 0x{addr+off:x} 0x{len(chunk):x} 0x{chunk.hex()}")

    def read_mem(self, addr, size):
        out = bytearray()
        step = 512
        for off in range(0, size, step):
            k = min(step, size - off)
            hexs = self.cmd(f"read 0x{addr+off:x} 0x{k:x}").split()[1][2:]
            out += bytes.fromhex(hexs)
        return bytes(out)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    if len(sys.argv) < 4:
        print("usage: run_rtl_qtest.py <qemu-system-arm> <lib.so> <vectors-dir> [N]")
        return 2
    qemu, libpath, vdir = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
    N = int(sys.argv[4]) if len(sys.argv) > 4 else 1024
    LOG2N = N.bit_length() - 1
    if (1 << LOG2N) != N:
        raise RuntimeError(f"N={N} is not a power of two")
    in_re = load_memh(os.path.join(vdir, f"in_re_{N}.memh"), N)
    in_im = load_memh(os.path.join(vdir, f"in_im_{N}.memh"), N)
    exp_re = load_memh(os.path.join(vdir, f"exp_re_{N}.memh"), N)
    exp_im = load_memh(os.path.join(vdir, f"exp_im_{N}.memh"), N)

    q = QTest(qemu, libpath)
    fails = []
    try:
        # 1. golden input into DRAM as interleaved int32 (re, im)
        blob = b"".join(struct.pack("<ii", in_re[i], in_im[i]) for i in range(N))
        q.write_mem(INPUT_ADDR, blob)
        # 2. configure and START -- this clocks the real Verilog
        q.writel(RTL_BASE + LOG2_N, LOG2N)
        q.writel(RTL_BASE + INPUT, INPUT_ADDR)
        q.writel(RTL_BASE + OUTPUT, OUTPUT_ADDR)
        q.writel(RTL_BASE + CONTROL, CTRL_START)
        st = q.readl(RTL_BASE + STATUS)
        if st & ST_ERROR:
            fails.append(f"device reported ERROR, code={q.readl(RTL_BASE + ERROR_CODE)}")
        if not (st & ST_DONE):
            fails.append("RTL block did not complete")
        # 3. read output, compare element-wise to golden
        out = q.read_mem(OUTPUT_ADDR, N * 8)
        got = struct.unpack("<" + "i" * (2 * N), out)
        mism = 0
        for i in range(N):
            if got[2 * i] != exp_re[i] or got[2 * i + 1] != exp_im[i]:
                if mism < 5:
                    fails.append(f"bin {i}: got ({got[2*i]},{got[2*i+1]}) "
                                 f"want ({exp_re[i]},{exp_im[i]})")
                mism += 1
        if mism == 0:
            print(f"Verilated RTL in QEMU: MATCH golden (N={N})")
        else:
            fails.append(f"{mism} bins diverged")
        # 4. sequence counter advanced
        if q.readl(RTL_BASE + RESULT_SEQ) != 1:
            fails.append("RESULT_SEQUENCE not advanced")
    finally:
        q.close()

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("P210_RTL_QEMU PASS (Verilated Verilog executes in the twin over MMIO/DMA, bit-exact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
