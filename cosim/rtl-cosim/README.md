# Run your own FPGA in the twin (RTL co-simulation)

This is the path for writing **custom Verilog** and running it as real RTL inside
the emulated P210. Your `.v` is compiled by [Verilator](https://verilator.org)
into a library; QEMU's `p210-rtl` device `dlopen()`s that library and clocks the
actual RTL cycle-by-cycle over the machine's MMIO/DMA. The datapath under test is
the Verilog itself, not a C model of it.

The worked example is the synthesizable, timing-closed FFT engine
[`../operator-rtl/p210_fft_synth.v`](../operator-rtl/p210_fft_synth.v) -- the same
RTL that closes 107 MHz on the real `xc7z020` in
[`../operator-rtl/synth/RESULTS.md`](../operator-rtl/synth/RESULTS.md).

## Prove it

```
make test        # standalone: Verilated RTL == golden, no QEMU   -> P210_RTL_COSIM PASS
make qemu-test   # same library driven INSIDE the twin over MMIO   -> P210_RTL_QEMU PASS
make check       # the above for the FFT AND a non-FFT custom block (examples/cmulj)
```

`make test` needs only `verilator` + a C compiler. `make qemu-test` and
`make check` additionally need the `p210-rtl` device built once:

```
sh ../../scripts/build_p210_qemu.sh     # macOS arm64
```

`make check` is the proof this path is genuinely customizable: it runs a second,
deliberately non-FFT block through the identical flow. See "Dropping in your own
block" below.

## How it fits together

```
your_block.v  --verilator-->  libyourblock.so  (exports the rtl_block ABI)
                                     |
        driver_test.c  ------------->|   standalone, no QEMU  (make test)
                                     |
        QEMU p210-rtl device --dlopen+MMIO/DMA-->  (make qemu-test)
        $P210_RTL_LIB names the .so; block sits at 0x7c450000 in the Zynq
```

Three files are the whole adapter:

| File | Role |
| --- | --- |
| [`rtl_block.h`](rtl_block.h) | The C ABI the software driver calls: `rtl_open / reset / load / start / run / read / close` + `rtl_log2n`. |
| [`fft_block_shim.cpp`](fft_block_shim.cpp) | Drives the Verilated DUT to satisfy that ABI (toggles `clk`, sequences load/start/done/read). You copy this and change the `DUT_CLASS`/`#include` to your block; `RTL_LOG2N` sets the size. |
| [`driver_test.c`](driver_test.c) | Block-agnostic: loads samples, starts, waits, reads, compares. Reused verbatim by the QEMU device. |

The Makefile is parameterized (`RTL`, `TOP`, `LIBNAME`, `SHIM`, `ROMS`, `VDIR`,
`N`), so a custom block builds through the same targets with only variable
overrides; each block gets its own `obj_$(LIBNAME)/` and never clobbers another.

## The block-processor interface

Your Verilog is a block processor: load N complex samples, pulse `start`, wait
for `done`, read N results. Your top must expose these exact port names (the shim
assigns them by name; different names mean editing the shim's port lines):

```verilog
module your_block #(parameter LOG2N = 10) (
    input  wire                clk,
    input  wire                rst,
    input  wire                start,      // pulse to begin
    output reg                 done,       // asserted when results are ready
    input  wire                ld_we,      // write-enable for host load
    input  wire [LOG2N-1:0]    io_addr,    // natural-order index for load/read
    input  wire signed [23:0]  ld_re, ld_im,
    output wire signed [23:0]  rd_re, rd_im // registered read (1-cycle addr latency)
);
```

Components are 24-bit signed (Q-format is yours to define). If your block reads
memory-init files via `$readmemh("name.memh")`, those resolve relative to the
process working directory; put them in this dir and pass `ROMS=name.memh`. A
block with no `$readmemh` (like the example) passes `ROMS=` (empty).

## Worked example: a non-FFT block

[`examples/cmulj`](examples/cmulj) is a deliberately non-FFT block -- multiply by
j (`out = j*in`, i.e. `out_re=-in_im, out_im=in_re`), no twiddle ROM. It exists
to prove the path is not FFT-specific. `make check` builds and runs both the FFT
and cmulj, standalone and in the twin:

```
make check
# P210_RTL_COSIM PASS ... / P210_RTL_QEMU PASS ...   (FFT, then cmulj)
```

cmulj is the template to copy. Its shim ([`examples/cmulj/cmulj_block_shim.cpp`](examples/cmulj/cmulj_block_shim.cpp))
differs from the FFT shim by exactly one line (the `DUT_CLASS`).

## Dropping in your own block

For a block in the **envelope** (complex, power-of-two N, equal N-in/N-out,
24-bit components, the port contract above):

1. Write `your_block.v`.
2. Copy `fft_block_shim.cpp` to `your_shim.cpp`; change `DUT_CLASS` and its
   `#include` to your Verilated class; set `RTL_LOG2N` to your `log2(N)`.
3. Generate vectors named `in_re_<N>.memh in_im_<N>.memh exp_re_<N>.memh
   exp_im_<N>.memh` in a dir (see `examples/cmulj/gen_cmulj_vectors.py`).
4. Prove it standalone, then in the twin, through the same targets:
   ```
   make test      RTL=your_block.v TOP=your_block LIBNAME=your_rtl \
                  SHIM=your_shim.cpp ROMS= VDIR=your/vectors N=1024
   make qemu-test RTL=your_block.v TOP=your_block LIBNAME=your_rtl \
                  SHIM=your_shim.cpp ROMS= VDIR=your/vectors N=1024
   ```
   The QEMU `p210-rtl` device is **not** rebuilt -- it dlopens your library.
   (For `N != 1024`, also pass `SHIM_DEFS=-DRTL_LOG2N=<log2 N>`.)

## Scope and envelope

This is a behavioral simulation of your RTL: it proves functional correctness and
that the block is drivable over the real register/DMA contract. It is not
gate-level, and timing is a separate claim (Vivado OOC synthesis, step 6 of
[`../REPRODUCE.md`](../REPRODUCE.md)). Different Verilator versions may schedule a
combinational block differently; drive your DUT through registered inputs/outputs
(as the examples do) so the ABI sees deterministic, synthesis-faithful behavior.

The turnkey path covers the **envelope**: complex, power-of-two `N`, equal
input/output length, 24-bit components, the port contract above. A block outside
it -- non-power-of-two size, rate-changing (`M != N` outputs), real-only or
reduction outputs, wider/narrower words, streaming ready/valid -- needs the ABI
(`rtl_load`/`rtl_read` are complex 24-bit) and the QEMU `p210-rtl` register map
(fixed N-in/N-out interleaved int32) widened first. That is a device + driver
change, not just a shim edit.
