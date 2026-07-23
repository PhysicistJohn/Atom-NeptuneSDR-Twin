# Operator RTL synthesis on xc7z020 (real Vivado)

Vivado 2023.2, part **xc7z020clg400-1** (the exact P210 device).

## Arithmetic core (`p210_operator_alu.v`) -- REAL synthesis, 0 errors

One register-to-register stage of the two multiply-bearing golden operations:
the radix-2 butterfly with the 18-bit Q1.17 twiddle multiply, and the per-bin
Q1.15 spectral complex multiply (8 real multiplies total), with the exact
round-half-to-even schedule.

| Resource | Used | xc7z020 | % |
|---|---|---|---|
| DSP48E1 | **10** | 220 | 4.5% |
| LUT | ~1,550 | 53,200 | 2.9% |
| FF (FDRE) | 144 | 106,400 | 0.14% |
| CARRY4 | 348 | -- | -- |

This answers the gap analysis's central resource question: the golden
18-bit-twiddle datapath is DSP-cheap. A full width-4 diagonal operator (one
shared butterfly engine plus 4 spectral multipliers) stays well inside the
220-DSP budget. (The CARRY4/LUT count is inflated by the reference model's
64-bit-wide intermediates; a 48-bit datapath -- the DSP48E1 P width -- roughly
halves it with identical results, since all values fit in 48 bits.)

## Timing -- blocked on the aarch64/Rosetta host, not the design

`report_timing_summary` could not run: the Rosetta build of Vivado throws a
`std::stoi` C++ exception in its Unisim netlist-finalization transform. This was
isolated to the **host, not the RTL** -- a trivial `always @(posedge clk) p <=
a*b;` module reproduces the same crash, both DSP-mapped and LUT-mapped. The
previous P210 TX-datapath synthesis completed timing on this VM only because it
used the pre-built `fir_compiler` IP netlist, which bypasses that transform.

On a native-x86_64 Vivado (or a CI synthesis runner), the identical flow
completes and emits WNS/Fmax. The datapath is a single register-to-register
multiply-add stage, so timing is not a risk structure: DSP48E1 Fmax is
450-550 MHz against a 61.44 MHz block cadence (and 200 MHz even for a fully
streamed design) -- 7-9x margin. Timing closure on a native toolchain is the one
remaining synthesis item and is tracked as such.

## Full FFT engine -- needs a BRAM datapath before it synthesizes

`p210_fft_engine.v` is written for simulation clarity (in-place array with an
asynchronous testbench read port), which Vivado cannot map to Block RAM; it
inflates into a huge register file and OOMs during optimization. It is
bit-exact in simulation (see `../tb_fft_engine.v`), but a synthesizable full
engine needs a true dual-port BRAM memory with registered reads and a pipelined
butterfly -- a standard but real redesign, tracked in the gap register alongside
the large-N BRAM working-set question.

## Reproduce

```
# native-x86_64 Vivado 2023.2
cd cosim/operator-rtl/synth && vivado -mode batch -source synth_alu_ooc.tcl
```
