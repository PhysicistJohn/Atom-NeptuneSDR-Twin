# Run your real Verilog as RTL inside the twin (Verilator).
#
#   make test       # build the block's library + run the standalone bit-exact driver
#   make qemu-test  # run the SAME library inside the QEMU twin over MMIO/DMA
#   make check      # both, for the bundled FFT AND the non-FFT cmulj example
#
# The defaults build the bundled FFT engine. To run YOUR OWN block, override the
# variables (see README.md, "Dropping in your own block"):
#
#   make test RTL=path/to/your.v TOP=your_top LIBNAME=your_rtl \
#            SHIM=path/to/your_shim.cpp ROMS= VDIR=path/to/vectors N=1024
#
# Each block builds into its own obj_$(LIBNAME)/, so blocks never clobber each
# other. A block that uses no $readmemh passes ROMS= (empty).

RTL       ?= ../operator-rtl/p210_fft_synth.v
TOP       ?= p210_fft_synth
LIBNAME   ?= p210fft_rtl
SHIM      ?= fft_block_shim.cpp
MDIR      ?= obj_$(LIBNAME)
# memh files the block needs at runtime (empty for a ROM-less block); copied
# from ROMSRC into this dir so the model's $readmemh resolves them.
ROMS      ?= rom_cos.memh rom_sin.memh
ROMSRC    ?= ../operator-rtl
# vectors + size for the tests (driver_test is generic on its filename args).
VDIR      ?= ../operator-rtl
N         ?= 1024
# extra shim -CFLAGS, e.g. -DRTL_LOG2N=8 for a differently sized block.
SHIM_DEFS ?=
VERILATOR ?= verilator
QEMU      ?= ../../.cache/qemu-p210/bin/qemu-system-arm

LIB = $(MDIR)/lib$(LIBNAME).a

.PHONY: lib test qemu-test check clean
lib: $(LIB)

$(LIB): $(RTL) $(SHIM) rtl_block.h $(ROMS)
	$(VERILATOR) --cc --build -O2 -Wno-fatal --Mdir $(MDIR) \
	    --lib-create $(LIBNAME) --top-module $(TOP) $(RTL) $(SHIM) \
	    -CFLAGS "-I$(CURDIR) $(SHIM_DEFS)"

# copy any needed memh into the run dir (fires only for the bare $(ROMS) names)
%.memh:
	cp $(ROMSRC)/$@ .

driver_test: driver_test.c lib
	cc -O2 -I. -o $@ driver_test.c $(LIB) -lstdc++ -lm

test: driver_test $(ROMS)
	./driver_test $(VDIR)/in_re_$(N).memh $(VDIR)/in_im_$(N).memh \
	              $(VDIR)/exp_re_$(N).memh $(VDIR)/exp_im_$(N).memh

qemu-test: lib $(ROMS)
	python3 ../tests/run_rtl_qtest.py "$(QEMU)" $(MDIR)/lib$(LIBNAME).so $(VDIR) $(N)

# Prove the customization path stays turnkey: the bundled FFT AND a genuinely
# different (non-FFT, ROM-less) block both pass -- standalone and in the twin --
# through this same parameterized flow, with only variable overrides.
EX = examples/cmulj
CMULJ = RTL=$(EX)/p210_cmulj.v TOP=p210_cmulj LIBNAME=p210cmulj_rtl \
        SHIM=$(EX)/cmulj_block_shim.cpp ROMS= VDIR=$(EX)/vectors
check: test qemu-test
	$(MAKE) test $(CMULJ)
	$(MAKE) qemu-test $(CMULJ)

clean:
	rm -rf obj_* driver_test rom_cos.memh rom_sin.memh
