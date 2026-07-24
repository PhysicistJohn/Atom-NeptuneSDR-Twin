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

## 5. Real FPGA synthesis (Vivado)

```
cd cosim/operator-rtl/synth && vivado -mode batch -source synth_alu_ooc.tcl
```

Out-of-context synthesis on the actual `xc7z020clg400-1`. Results and the timing
progression (43.6 MHz unpipelined -> 107.3 MHz pipelined, clearing the 61.44 MHz
cadence) are in [`synth/RESULTS.md`](operator-rtl/synth/RESULTS.md), including
the native-vs-Rosetta method note.

## Scope

The QEMU device is a functional twin, bit-exact to the golden reference; it is
not evidence of a synthesized, timing-closed bitstream (that is the RTL +
`synth/RESULTS.md`). See the parent README for the v1 machine and the firmware
bundle.
