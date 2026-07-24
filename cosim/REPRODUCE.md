# Reproducing the operator chain

The P210 spectral neural operator is one arithmetic (golden-arithmetic v1)
reproduced bit-for-bit by four implementations, plus real FPGA synthesis. Every
step below is self-contained and runnable; the golden `.memh`/ROM vectors are
committed so nothing needs the sibling repo unless you are regenerating them.

The single source of truth is the spec:
[`Atom-NeptuneSDR-Firmware/specs/golden-arithmetic-v1.md`](https://github.com/PhysicistJohn/Atom-NeptuneSDR-Firmware/blob/main/specs/golden-arithmetic-v1.md),
whose pinned test-vector digests every implementation reproduces.

## 1. Python reference (Atom-Neural-RL)

```
cd Atom-Neural-RL && PYTHONPATH=src python3 -m unittest tests.test_golden
```

The normative reference (`golden.py`) and its four pinned vector digests.

## 2. C core + v2 ABI device (this repo)

```
cd cosim/operator-core && make test
```

Proves the pure-C golden core, and the v2 register/DMA device over it, reproduce
the pinned digests (`P210_OPERATOR_CORE_BITEXACT PASS`,
`P210_OPERATOR_DEVICE_ABI PASS`). Needs `cc` and `zlib`.

## 3. RTL simulation (this repo)

```
cd cosim/operator-rtl && make sim      # needs iverilog
```

The FFT engine (`p210_fft_engine.v`) and the synthesizable, timing-closed engine
(`p210_fft_synth.v`) match the golden vectors bit-for-bit. `make gen` regenerates
the committed `.memh` from Atom-Neural-RL if the arithmetic ever changes.

## 4. The operator running IN the QEMU twin (this repo)

```
sh scripts/build_p210_qemu.sh          # macOS arm64; ~15-25 min clean, instant cached
python3 cosim/tests/run_operator_qtest.py \
    .cache/qemu-p210/bin/qemu-system-arm \
    ../Atom-Neural-RL/src/atom_neural_rl/data/twiddle-rom-q117.bin
```

Builds QEMU 10.0.2 with the operator device (enabled by the `p210-operator`
machine flag, which maps it at the real accelerator address 0x7c450000; the
default `p210=on` leaves the v1 FFT there instead), then drives it through
emulated MMIO + DMA (no guest boot, via qtest): the operator
executes the full ABI transaction and its block output matches the golden pin
(`P210_OPERATOR_QEMU PASS`), with the CRC gate rejecting a corrupted bank.

And driven from the emulated **CPU** (not external qtest), a bare-metal ARM
program that pokes the operator registers itself:

```
cd cosim/qemu-baremetal && make run QEMU=../../.cache/qemu-p210/bin/qemu-system-arm
```

`P210_OPERATOR_BAREMETAL PASS` -- the operator driven from code on the guest
Cortex-A9, bit-exact. (Needs an arm-none-eabi toolchain.)

## 5. Your own RTL running IN the twin (Verilator co-sim)

The device in step 4 is a C transliteration of the golden arithmetic. This step
runs the **actual Verilog** instead. The synthesizable engine
(`operator-rtl/p210_fft_synth.v`, the same RTL that closes timing in step 6) is
Verilated into a library and driven cycle-by-cycle inside the emulated Zynq.

```
cd cosim/rtl-cosim && make test        # needs verilator; standalone bit-exact proof
```

`P210_RTL_COSIM PASS` -- the Verilated RTL matches golden through the `rtl_block`
ABI, with no QEMU. Then the same library, executing in the twin over MMIO/DMA:

```
sh scripts/build_p210_qemu.sh          # once; builds the p210-rtl device
python3 cosim/tests/run_rtl_qtest.py \
    .cache/qemu-p210/bin/qemu-system-arm \
    cosim/rtl-cosim/obj_p210fft_rtl/libp210fft_rtl.so \
    cosim/operator-rtl
```

`P210_RTL_QEMU PASS` -- QEMU's `p210-rtl` machine flag maps a dlopen-backed RTL
block at 0x7c450000; it loads the library named by `$P210_RTL_LIB`, DMAs the
input in, clocks the real Verilog to `done`, and DMAs the result out, bit-exact
to the golden pin.

**To run your own FPGA**, `make check` proves the flow end to end on a second,
deliberately non-FFT block (`examples/cmulj`, multiply-by-j) built through the
same variables:

```
cd cosim/rtl-cosim && make check       # FFT + a custom block, standalone + in the twin
```

Writing your own block means: author a small block (same block-processor port
contract), copy the ~90-line shim and swap one class name, generate vectors, then
`make test` / `make qemu-test` with your `RTL=/TOP=/LIBNAME=/SHIM=/VDIR=`
overrides. The QEMU `p210-rtl` device is **not** rebuilt -- it dlopens whatever
library you name. The full recipe, the port contract, and the size/word-width
envelope are in [`rtl-cosim/README.md`](rtl-cosim/README.md).

## 6. Real FPGA synthesis (Vivado)

```
cd cosim/operator-rtl/synth && vivado -mode batch -source synth_alu_ooc.tcl
```

Out-of-context synthesis on the actual `xc7z020clg400-1`. Results and the timing
progression (43.6 MHz unpipelined -> 107.3 MHz pipelined, clearing the 61.44 MHz
cadence) are in [`synth/RESULTS.md`](operator-rtl/synth/RESULTS.md), including
the native-vs-Rosetta method note.

## Scope

Three distinct claims, kept separate:

- **Functional twin** (step 4): the `p210-operator` device is a C model of the
  golden arithmetic, bit-exact to the reference.
- **RTL co-simulation** (step 5): the `p210-rtl` device runs the real Verilog
  (Verilated) inside the twin. This proves the RTL is functionally correct and
  drivable over the register/DMA ABI. It is a behavioral simulation of the RTL,
  not gate-level and not timing.
- **Timing closure** (step 6): out-of-context Vivado synthesis on the actual
  `xc7z020clg400-1` establishes the clock the RTL meets. It is not a placed,
  routed, full-device bitstream.

See the parent README for the v1 machine and the firmware bundle.
