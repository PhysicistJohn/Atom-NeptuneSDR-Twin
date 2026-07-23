/*
 * P210 v2 operator MMIO device -- register/DMA state machine over the golden
 * core. See p210_operator_device.h. SPDX-License-Identifier: MIT
 */
#include "p210_operator_device.h"
#include "p210_operator_core.h"
#include <stdlib.h>
#include <string.h>
#include <zlib.h>   /* crc32; the standalone test provides a tiny fallback */

static uint32_t idx(uint32_t offset) { return (offset & 0x3ff) >> 2; }

void p210_dev_reset(P210OperatorDevice *d)
{
    uint8_t *ddr = d->ddr; size_t base = d->ddr_base, size = d->ddr_size;
    int16_t *wr = d->wt_re, *wi = d->wt_im;
    memset(d->reg, 0, sizeof d->reg);
    d->ddr = ddr; d->ddr_base = base; d->ddr_size = size;
    d->wt_re = wr; d->wt_im = wi; d->wt_modes = 0;
    /* reset state = v1 bypass + power output (BYPASS set) */
    d->reg[idx(P210_REG_OP_FLAGS)] = 1;             /* BYPASS */
    d->reg[idx(P210_REG_OP_OUTPUT_MODE)] = P210_OUT_POWER;
}

uint32_t p210_dev_read32(P210OperatorDevice *d, uint32_t offset)
{
    return d->reg[idx(offset)];
}

/* DMA helpers over the flat DDR model (dma_memory_read/write in QEMU). */
static void *dma_ptr(P210OperatorDevice *d, uint32_t addr, size_t bytes)
{
    size_t off = (size_t)addr - d->ddr_base;
    if (addr < d->ddr_base || off + bytes > d->ddr_size) return NULL;
    return d->ddr + off;
}

/* Capture the weight table from DDR at ACTIVATE, after CRC verification. */
static int activate_bank(P210OperatorDevice *d)
{
    uint32_t addr  = d->reg[idx(P210_REG_WEIGHT_ADDR)];
    uint32_t bytes = d->reg[idx(P210_REG_WEIGHT_BYTES)];
    uint32_t want  = d->reg[idx(P210_REG_WEIGHT_CRC)];
    uint8_t *blob = dma_ptr(d, addr, bytes);
    if (!blob) { d->reg[idx(P210_REG_ERROR_CODE)] = P210_ERR_BANK_INTEGRITY;
                 d->reg[idx(P210_REG_STATUS)] |= P210_ST_BANK_CRC_FAIL; return -1; }
    uint32_t got = (uint32_t)crc32(0, blob, bytes);
    if (got != want) { d->reg[idx(P210_REG_ERROR_CODE)] = P210_ERR_BANK_INTEGRITY;
                       d->reg[idx(P210_REG_STATUS)] |= P210_ST_BANK_CRC_FAIL; return -1; }
    /* blob layout (this device's bank): [modes:u32][re:int16*modes][im:int16*modes] */
    uint32_t modes; memcpy(&modes, blob, 4);
    free(d->wt_re); free(d->wt_im);
    d->wt_re = malloc(modes * sizeof(int16_t));
    d->wt_im = malloc(modes * sizeof(int16_t));
    memcpy(d->wt_re, blob + 4, modes * sizeof(int16_t));
    memcpy(d->wt_im, blob + 4 + modes * sizeof(int16_t), modes * sizeof(int16_t));
    d->wt_modes = modes;
    d->reg[idx(P210_REG_ACTIVE_WT_CRC)] = got;
    d->reg[idx(P210_REG_STATUS)] |= P210_ST_WEIGHT_READY;
    d->reg[idx(P210_REG_STATUS)] &= ~P210_ST_BANK_CRC_FAIL;
    return 0;
}

/* Execute one block: FFT -> spectral multiply -> IFFT -> modReLU (single-channel
 * diagonal operator), reading input from DDR and writing complex output. */
static int run_block(P210OperatorDevice *d)
{
    uint32_t log2n = d->reg[idx(P210_REG_LOG2_N)];
    uint32_t n = 1u << log2n;
    if (d->reg[idx(P210_REG_OP_FLAGS)] & 1u) {   /* BYPASS: pass input through */
        uint32_t ia = d->reg[idx(P210_REG_INPUT_ADDR)];
        uint32_t oa = d->reg[idx(P210_REG_OUTPUT2_ADDR)];
        void *src = dma_ptr(d, ia, n * 8), *dst = dma_ptr(d, oa, n * 8);
        if (!src || !dst) return -1;
        memcpy(dst, src, n * 8);
        d->reg[idx(P210_REG_STATUS)] |= P210_ST_DONE;
        return 0;
    }
    if (d->wt_modes != n) { d->reg[idx(P210_REG_ERROR_CODE)] = P210_ERR_BAD_OP_CONFIG;
                            d->reg[idx(P210_REG_STATUS)] |= P210_ST_ERROR; return -1; }
    int32_t *in = dma_ptr(d, d->reg[idx(P210_REG_INPUT_ADDR)], n * 8);   /* int32 re,im interleaved */
    int32_t *out = dma_ptr(d, d->reg[idx(P210_REG_OUTPUT2_ADDR)], n * 8);
    if (!in || !out) return -1;

    int64_t *re = malloc(n * sizeof(int64_t)), *im = malloc(n * sizeof(int64_t));
    for (uint32_t i = 0; i < n; i++) { re[i] = in[2*i]; im[i] = in[2*i+1]; }
    p210_op_fft(re, im, n);
    p210_op_spectral_multiply(re, im, d->wt_re, d->wt_im, n);
    p210_op_ifft(re, im, n);
    int32_t b_q23 = (int32_t)d->reg[idx(P210_REG_OP_THRESHOLD)];
    p210_op_modrelu(re, im, n, b_q23, (int)log2n);   /* block_exp = log2n */
    for (uint32_t i = 0; i < n; i++) { out[2*i] = (int32_t)re[i]; out[2*i+1] = (int32_t)im[i]; }
    free(re); free(im);

    d->reg[idx(P210_REG_RESULT_SEQUENCE)]++;
    d->reg[idx(P210_REG_RESULT_WT_CRC)] = d->reg[idx(P210_REG_ACTIVE_WT_CRC)];  /* attribution */
    d->reg[idx(P210_REG_STATUS)] |= P210_ST_DONE;
    return 0;
}

void p210_dev_write32(P210OperatorDevice *d, uint32_t offset, uint32_t value)
{
    if (offset == P210_REG_CONTROL) {
        if (value & P210_CTRL_SOFT_RESET) { p210_dev_reset(d); return; }
        if (value & P210_CTRL_BYPASS_SELECT) d->reg[idx(P210_REG_OP_FLAGS)] |= 1u;
        if (value & P210_CTRL_WEIGHT_ACTIVATE) activate_bank(d);
        if (value & P210_CTRL_START) {
            d->reg[idx(P210_REG_STATUS)] &= ~(P210_ST_DONE | P210_ST_ERROR);
            run_block(d);
        }
        return;
    }
    d->reg[idx(offset)] = value;
    /* clearing BYPASS via OP_FLAGS enables the operator path */
}
