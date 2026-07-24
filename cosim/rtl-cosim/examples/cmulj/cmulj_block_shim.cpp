// Shim for the cmulj example: a verbatim copy of ../../fft_block_shim.cpp with
// only the two documented edits made -- DUT_CLASS and the #include point at this
// block's Verilated class (Vp210_cmulj). RTL_LOG2N stays 10 because cmulj is a
// 1024-point block like the FFT. This file IS the "copy the shim, swap the
// class" template the README describes. SPDX-License-Identifier: MIT
#include "rtl_block.h"
#include "Vp210_cmulj.h"
#include "verilated.h"

/* The Verilated top the shim drives -- the one line that differs from the FFT. */
#define DUT_CLASS Vp210_cmulj

/* log2 of the block size (address width). Override with -DRTL_LOG2N=k. */
#ifndef RTL_LOG2N
#define RTL_LOG2N 10
#endif
#define RTL_ADDR_MASK ((1u << RTL_LOG2N) - 1u)

/* Verilator's inline vl_time_stamp64() calls sc_time_stamp(); this model is
 * untimed (no $time), so a trivial hook lets a static link resolve. */
double sc_time_stamp() { return 0; }

struct rtl_block {
    VerilatedContext *ctx;
    DUT_CLASS *dut;
};

static void tick(rtl_block *b)
{
    b->dut->clk = 0; b->dut->eval();
    b->dut->clk = 1; b->dut->eval();
}

static int32_t sext24(uint32_t v) { return (int32_t)(v << 8) >> 8; }

extern "C" {

rtl_block *rtl_open(void)
{
    rtl_block *b = new rtl_block();
    b->ctx = new VerilatedContext();
    b->dut = new DUT_CLASS(b->ctx);
    b->dut->rst = 1; b->dut->start = 0; b->dut->ld_we = 0;
    for (int i = 0; i < 4; i++) tick(b);
    b->dut->rst = 0; tick(b);
    return b;
}

void rtl_reset(rtl_block *b)
{
    b->dut->rst = 1;
    for (int i = 0; i < 4; i++) tick(b);
    b->dut->rst = 0; tick(b);
}

void rtl_load(rtl_block *b, uint32_t idx, int32_t re, int32_t im)
{
    b->dut->ld_we = 1;
    b->dut->io_addr = idx & RTL_ADDR_MASK;
    b->dut->ld_re = (uint32_t)re & 0xffffff;
    b->dut->ld_im = (uint32_t)im & 0xffffff;
    tick(b);
    b->dut->ld_we = 0;
}

void rtl_start(rtl_block *b, uint32_t log2n)
{
    (void)log2n;                 /* fixed-size block */
    b->dut->start = 1; tick(b);
    b->dut->start = 0;
}

int rtl_run(rtl_block *b, uint64_t max_cycles)
{
    for (uint64_t i = 0; i < max_cycles; i++) {
        tick(b);
        if (b->dut->done) return 0;
    }
    return -1;
}

void rtl_read(rtl_block *b, uint32_t idx, int32_t *re, int32_t *im)
{
    b->dut->io_addr = idx & RTL_ADDR_MASK;
    tick(b); tick(b);            /* addr register + synchronous RAM read */
    *re = sext24(b->dut->rd_re);
    *im = sext24(b->dut->rd_im);
}

void rtl_close(rtl_block *b)
{
    delete b->dut; delete b->ctx; delete b;
}

uint32_t rtl_log2n(void) { return RTL_LOG2N; }

}
