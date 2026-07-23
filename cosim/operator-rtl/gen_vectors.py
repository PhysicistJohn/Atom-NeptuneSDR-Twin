#!/usr/bin/env python3
"""Regenerate the golden .memh vectors for the operator RTL testbenches.

The committed .memh files (twiddle ROM cos/sin, and golden FFT input/expected at
N=256 and N=1024) are derived from the Atom-Neural-RL golden reference. They are
committed so `make sim` needs only iverilog; run this only if the golden
arithmetic changes. Point ANRL_SRC at the sibling repo's src if it is not the
default sibling path.
"""
import os
import sys

ANRL_SRC = os.environ.get(
    "ANRL_SRC",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "Atom-Neural-RL", "src"),
)
sys.path.insert(0, os.path.abspath(ANRL_SRC))

import numpy as np
from atom_neural_rl import golden  # noqa: E402


def memh(path, vals, bits):
    mask = (1 << bits) - 1
    width = (bits + 3) // 4
    with open(path, "w") as f:
        for v in np.asarray(vals, dtype=np.int64):
            f.write(f"{int(v) & mask:0{width}x}\n")


def main():
    rom = golden.load_twiddle_rom()
    memh("rom_cos.memh", rom[0::2], 18)
    memh("rom_sin.memh", rom[1::2], 18)
    for n in (256, 1024):
        re, im = golden.vector_input(seed={256: 11, 1024: 12}[n], n=n)
        yr, yi = golden.golden_fft(re, im)
        memh(f"in_re_{n}.memh", re, 24)
        memh(f"in_im_{n}.memh", im, 24)
        memh(f"exp_re_{n}.memh", yr, 24)
        memh(f"exp_im_{n}.memh", yi, 24)
    print("regenerated: rom_cos/sin.memh, in/exp_{re,im}_{256,1024}.memh")


if __name__ == "__main__":
    main()
