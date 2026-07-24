# Build and run the bare-metal operator drive on the emulated Cortex-A9.
#   make run QEMU=../../.cache/qemu-p210/bin/qemu-system-arm
# Proves code running on the guest CPU drives the operator at 0x7c450000 via
# MMIO/DMA, bit-exact -- not just external qtest.

CROSS  ?= arm-none-eabi-
CC      = $(CROSS)gcc
QEMU   ?= ../../.cache/qemu-p210/bin/qemu-system-arm
CFLAGS  = -mcpu=cortex-a9 -marm -ffreestanding -O2 -Wall -nostdlib

.PHONY: run clean
op_test.elf: start.S main.c link.ld expected_op256.h
	$(CC) $(CFLAGS) -T link.ld start.S main.c -o $@ -lgcc

run: op_test.elf
	$(QEMU) -machine xilinx-zynq-a9,p210=on,p210-operator=on -m 1024 \
	    -nographic -no-reboot -semihosting -kernel op_test.elf

clean:
	rm -f op_test.elf
