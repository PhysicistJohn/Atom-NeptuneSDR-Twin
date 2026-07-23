/*
 * P210 v2 operator MMIO device -- the register/DMA state machine.
 *
 * This models the ABI-visible behaviour of the operator accelerator: the v2
 * register map (specs/p210-firmware-interface-v2.json), weight-bank load with a
 * CRC gate, ACTIVATE, block execution reading/writing a DDR model, and the
 * per-result weight attribution. Compute is the proven golden-arithmetic core
 * (p210_operator_core). This is the substance a QEMU sysbus device wraps: the
 * MMIO read/write/reset handlers are thin adapters over op_dev_write32/read32,
 * and dma_memory_read/write replace the flat DDR pointer used here for the
 * standalone equivalence test.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef P210_OPERATOR_DEVICE_H
#define P210_OPERATOR_DEVICE_H

#include <stdint.h>
#include <stddef.h>

/* v2 register offsets (subset that the operator mode uses). */
#define P210_REG_CONTROL         0x00c
#define P210_REG_STATUS          0x010
#define P210_REG_ERROR_CODE      0x014
#define P210_REG_LOG2_N          0x018
#define P210_REG_INPUT_ADDR      0x024
#define P210_REG_OUTPUT_ADDR     0x02c
#define P210_REG_RESULT_SEQUENCE 0x038
#define P210_REG_OP_LAYERS       0x080
#define P210_REG_OP_MODE_COUNT   0x084
#define P210_REG_OP_OUTPUT_MODE  0x088
#define P210_REG_OP_FLAGS        0x08c
#define P210_REG_WEIGHT_ADDR     0x090
#define P210_REG_WEIGHT_BYTES    0x094
#define P210_REG_WEIGHT_CRC      0x0a0
#define P210_REG_ACTIVE_WT_CRC   0x0a4
#define P210_REG_RESULT_WT_CRC   0x0ac
#define P210_REG_OUTPUT2_ADDR    0x0dc
#define P210_REG_OP_THRESHOLD    0x0e4   /* Q1.23 modReLU threshold (operator ext) */

/* CONTROL bits. */
#define P210_CTRL_START          0x00000001u
#define P210_CTRL_SOFT_RESET     0x00000002u
#define P210_CTRL_BYPASS_SELECT  0x00000200u
#define P210_CTRL_WEIGHT_ACTIVATE 0x00000400u
/* STATUS bits. */
#define P210_ST_DONE             0x00000002u
#define P210_ST_ERROR            0x00000004u
#define P210_ST_WEIGHT_READY     0x00000010u
#define P210_ST_BANK_CRC_FAIL    0x00000020u
/* OUTPUT_MODE. */
#define P210_OUT_POWER           0u
#define P210_OUT_COMPLEX         1u
#define P210_OUT_BOTH            2u
/* error codes. */
#define P210_ERR_NONE            0u
#define P210_ERR_BANK_INTEGRITY  11u
#define P210_ERR_BAD_OP_CONFIG   12u

typedef struct {
    uint32_t reg[0x100];        /* register file, word-indexed by offset/4 kept simple */
    uint8_t *ddr;               /* flat DDR model (the "DMA" target) */
    size_t   ddr_base;          /* physical base the addresses are relative to */
    size_t   ddr_size;
    /* loaded weight table (Q1.15 mantissas), captured at ACTIVATE */
    int16_t *wt_re;
    int16_t *wt_im;
    uint32_t wt_modes;
} P210OperatorDevice;

void     p210_dev_reset(P210OperatorDevice *d);
uint32_t p210_dev_read32(P210OperatorDevice *d, uint32_t offset);
void     p210_dev_write32(P210OperatorDevice *d, uint32_t offset, uint32_t value);

#endif /* P210_OPERATOR_DEVICE_H */
