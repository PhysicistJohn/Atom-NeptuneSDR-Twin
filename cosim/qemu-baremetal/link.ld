/* Bare-metal image in Zynq DDR, clear of the operator's DMA buffers
 * (0x18000000+). SPDX-License-Identifier: MIT */
ENTRY(_start)
MEMORY { ram (rwx) : ORIGIN = 0x00100000, LENGTH = 0x00200000 }
SECTIONS {
    .text : { *(.vectors) *(.text*) } > ram
    .rodata : { *(.rodata*) } > ram
    .data : { *(.data*) } > ram
    .bss : { *(.bss*) *(COMMON) } > ram
    . = ALIGN(8);
    . = . + 0x10000;
    _stack_top = .;
}
