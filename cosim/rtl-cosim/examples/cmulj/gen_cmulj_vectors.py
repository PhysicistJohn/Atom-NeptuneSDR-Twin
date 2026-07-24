#!/usr/bin/env python3
"""Generate cmulj golden vectors from the committed FFT inputs.

Reuses ../../../operator-rtl/in_re_1024.memh and in_im_1024.memh as input, and
computes the multiply-by-j expected output: exp_re = -in_im, exp_im = in_re,
written as 24-bit hex .memh in the same format the driver/harness parse.
"""
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "operator-rtl")
OUT = os.path.join(os.path.dirname(__file__), "vectors")
N = 1024
MASK = (1 << 24) - 1


def load(path):
    vals = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            v = int(line, 16) & MASK
            vals.append(v - (1 << 24) if v & 0x800000 else v)  # sign-extend
            if len(vals) == N:
                break
    assert len(vals) == N, (path, len(vals))
    return vals


def write(path, vals):
    with open(path, "w") as f:
        for v in vals:
            f.write(f"{v & MASK:06x}\n")


def main():
    os.makedirs(OUT, exist_ok=True)
    in_re = load(os.path.join(SRC, "in_re_1024.memh"))
    in_im = load(os.path.join(SRC, "in_im_1024.memh"))
    exp_re = [-in_im[i] for i in range(N)]   # out_re = -in_im
    exp_im = [ in_re[i] for i in range(N)]   # out_im =  in_re
    # copy inputs into the vectors dir under the names the harness expects
    write(os.path.join(OUT, "in_re_1024.memh"), in_re)
    write(os.path.join(OUT, "in_im_1024.memh"), in_im)
    write(os.path.join(OUT, "exp_re_1024.memh"), exp_re)
    write(os.path.join(OUT, "exp_im_1024.memh"), exp_im)
    print("wrote in/exp_{re,im}_1024.memh to", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
