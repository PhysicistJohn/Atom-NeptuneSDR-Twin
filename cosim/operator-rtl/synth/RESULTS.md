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

## Full synthesizable FFT engine (`p210_fft_synth.v`) -- REAL synthesis, 0 errors

The engine rewritten for synthesis: a true dual-port BRAM sample memory with
synchronous reads, in-place radix-2 DIT, one butterfly per multi-cycle pass.
Bit-exact to golden at N=1024 in simulation (`tb_fft_synth.v`,
P210_FFT_SYNTH_BITEXACT PASS). Out-of-context synthesis on xc7z020clg400-1,
LOG2N=10:

| Resource | Used | xc7z020 | % |
|---|---|---|---|
| RAMB36E1 | 38 | 140 | 27% |
| DSP48E1 | 4 | 220 | 1.8% |
| LUT | **~920** | 53,200 | 1.7% |
| FF (FDRE) | 164 | 106,400 | 0.2% |

Address generation is incremental (per-stage constants maintained by
doubling/halving, `ia`/`ib` as adders, `tw_idx` as an accumulator), so there are
no per-butterfly variable shifts. This cut LUTs from ~14,000 (the first,
shift-based version) to ~920, and moved the twiddle ROM out of LUTs into Block
RAM where it belongs. BRAM is now dominated by the full-resolution twiddle ROM
(32768 half-turn entries, sized for N-invariance up to 65536) plus the sample
memory; a fixed-small-N deployment could subsample the ROM to a few BRAM. Fits
the device (38/140 BRAM). Still bit-exact to golden (tb_fft_synth.v PASS).

A full single-channel operator = this engine (2 BRAM, 4 DSP) + the spectral
multiply and modReLU (a few more DSP, see the ALU) + weight-table BRAM -- a
small fraction of the 220-DSP / 140-BRAM device. The resource-feasibility
question is now answered by real synthesis of the actual engine, not estimate.

## Native-x86_64 VM for timing -- tested, Vivado not installed there

The stopped native-x86_64 lima VM (`neptune-vivado-2023-2`) was booted and
checked: it runs x86_64 natively (no Rosetta stoi bug) but does **not** have
Vivado installed -- only the aarch64/Rosetta VM does. Getting WNS/Fmax therefore
needs either a multi-hour Vivado install on that VM or an x86 CI synthesis
runner. The synthesized structures (BRAM-registered memory, single
register-to-register DSP butterfly) are not timing-risk shapes at the 61.44 MHz
block cadence.

## Real timing closure -- native x86 Vivado (Rosetta stoi bug avoided)

The Rosetta build of Vivado crashes in the Unisim netlist transform, so a full
29 GB Vivado install was copied VM-to-VM to a native-x86_64 lima VM (both target
x86-64). Native Vivado sailed past the exact transform that crashed under
Rosetta ("Unisim Transformation completed", no std::stoi) -- confirming that bug
was Rosetta-specific -- and produced real WNS/Fmax.

**The first synthesis proved the engine did NOT close timing.** The unpipelined
butterfly (BRAM read -> 24x18 multiply -> round-half-even -> add -> round ->
clamp -> BRAM write, all combinational in one clock) has a ~23 ns critical path:

| Engine | Fmax | vs 61.44 MHz cadence |
|---|---|---|
| unpipelined | **43.6 MHz** (WNS -17.95 ns @ 5 ns) | FAILS (below cadence) |
| pipelined (3-stage butterfly) | **107.3 MHz** (WNS -4.32 ns @ 5 ns) | PASSES, 1.75x margin |

The pipeline registers the four DSP products (S_P1), the rounded twiddle results
(S_P2), and the final add/round/clamp+write (S_WR) into separate stages -- still
bit-exact to golden (tb_fft_synth.v PASS). Pipelined resource: 4 DSP48E1,
38 RAMB36, ~950 LUT, 368 FF, 0 errors.

107 MHz clears the 61.44 MHz sample clock the block-cadence operator runs at
(throughput is multi-cycle by design, not continuous streaming). It does not
reach 200 MHz -- the remaining ~9 ns path (the round-half-even logic after the
DSP) would need one more register stage, which is a straightforward addition if
a higher clock is ever wanted. This is real timing closure at the target
cadence, measured on the actual part, not asserted.
