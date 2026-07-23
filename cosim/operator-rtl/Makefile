# Build and run the operator RTL simulations (iverilog). Bit-exact to golden.
#
#   make sim        # run both FFT engine testbenches against the golden vectors
#   make gen        # regenerate the golden .memh vectors from Atom-Neural-RL
#
# The .memh vectors (twiddle ROM + golden input/expected) are committed, so
# `make sim` runs out of the box with only iverilog installed. `make gen`
# regenerates them from the sibling Atom-Neural-RL golden reference.

IVERILOG ?= iverilog

.PHONY: sim engine synth gen clean
sim: engine synth

engine: tb_fft
	./tb_fft

synth: tb_synth
	./tb_synth

tb_fft: p210_fft_engine.v tb_fft_engine.v
	$(IVERILOG) -g2012 -o $@ $^

tb_synth: p210_fft_synth.v tb_fft_synth.v
	$(IVERILOG) -g2012 -o $@ $^

gen:
	python3 gen_vectors.py

clean:
	rm -f tb_fft tb_synth tb_fft_engine *.vcd
