# Build and run the operator golden-arithmetic C core equivalence tests.
#
#   make test ROM=/path/to/twiddle-rom-q117.bin
#
# ROM defaults to the sibling Atom-Neural-RL committed twiddle ROM. The tests
# prove the C core (and the v2 register/DMA device over it) reproduce the golden
# pinned vectors bit-for-bit -- the same vectors the Python reference and RTL match.

CC ?= cc
CFLAGS ?= -O2 -Wall
ROM ?= ../../../Atom-Neural-RL/src/atom_neural_rl/data/twiddle-rom-q117.bin

.PHONY: test clean
test: test_operator_core test_operator_device
	./test_operator_core $(ROM)
	./test_operator_device $(ROM)

test_operator_core: test_operator_core.c p210_operator_core.c
	$(CC) $(CFLAGS) -o $@ $^

test_operator_device: test_operator_device.c p210_operator_device.c p210_operator_core.c
	$(CC) $(CFLAGS) -o $@ $^ -lz

clean:
	rm -f test_operator_core test_operator_device
