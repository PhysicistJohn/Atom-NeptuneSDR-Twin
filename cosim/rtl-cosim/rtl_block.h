/*
 * Custom-FPGA-on-the-twin: the C ABI a Verilated RTL block exposes.
 *
 * Write your block in Verilog with a block-processor interface (load N complex
 * samples, start, wait for done, read N results), Verilate it against this shim
 * pattern, and the twin's RTL co-processor device drives your real RTL through
 * MMIO/DMA -- no C re-implementation. p210_fft_synth.v is the worked example.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef RTL_BLOCK_H
#define RTL_BLOCK_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rtl_block rtl_block;

/* Create + reset the Verilated model. */
rtl_block *rtl_open(void);
void       rtl_reset(rtl_block *b);
/* Load one complex sample at index idx (24-bit signed components). */
void       rtl_load(rtl_block *b, uint32_t idx, int32_t re, int32_t im);
/* Kick the block for a 2^log2n-point transform (fixed blocks may ignore it). */
void       rtl_start(rtl_block *b, uint32_t log2n);
/* Clock until the block asserts done; returns 0 on done, -1 on timeout. */
int        rtl_run(rtl_block *b, uint64_t max_cycles);
/* Read one complex result at index idx (sign-extended to 32 bits). */
void       rtl_read(rtl_block *b, uint32_t idx, int32_t *re, int32_t *im);
void       rtl_close(rtl_block *b);
/* The fixed transform size the block was built for (log2 N). */
uint32_t   rtl_log2n(void);

#ifdef __cplusplus
}
#endif

#endif /* RTL_BLOCK_H */
