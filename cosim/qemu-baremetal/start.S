/* Minimal Cortex-A9 bare-metal startup: set the stack, call main, spin.
 * SPDX-License-Identifier: MIT */
    .section .vectors, "ax"
    .global _start
_start:
    ldr sp, =_stack_top
    bl  main
1:  b   1b
