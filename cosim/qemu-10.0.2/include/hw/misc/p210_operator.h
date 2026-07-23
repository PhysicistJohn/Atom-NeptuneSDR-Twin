/*
 * P210 v2 spectral neural-operator PL device (QEMU).
 *
 * A firmware-visible operator accelerator that executes golden-arithmetic v1
 * (Atom-Neural-RL golden.py / specs/golden-arithmetic-v1.md): a single-channel
 * diagonal operator FFT -> spectral multiply -> IFFT -> modReLU, driven through
 * the v2 register/DMA ABI. It is a functional QEMU implementation, bit-exact to
 * the golden reference, not evidence of synthesized RTL.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef HW_MISC_P210_OPERATOR_H
#define HW_MISC_P210_OPERATOR_H

#include "hw/sysbus.h"
#include "qom/object.h"

#define TYPE_P210_OPERATOR "p210-operator"
OBJECT_DECLARE_SIMPLE_TYPE(P210OperatorState, P210_OPERATOR)

#define P210_OPERATOR_MMIO_SIZE 0x1000

/* v2 register offsets (operator subset). */
#define P210_OP_REG_CONTROL         0x00c
#define P210_OP_REG_STATUS          0x010
#define P210_OP_REG_ERROR_CODE      0x014
#define P210_OP_REG_LOG2_N          0x018
#define P210_OP_REG_INPUT_ADDR      0x024
#define P210_OP_REG_RESULT_SEQUENCE 0x038
#define P210_OP_REG_OP_MODE_COUNT   0x084
#define P210_OP_REG_OP_OUTPUT_MODE  0x088
#define P210_OP_REG_OP_FLAGS        0x08c
#define P210_OP_REG_WEIGHT_ADDR     0x090
#define P210_OP_REG_WEIGHT_BYTES    0x094
#define P210_OP_REG_WEIGHT_CRC      0x0a0
#define P210_OP_REG_ACTIVE_WT_CRC   0x0a4
#define P210_OP_REG_RESULT_WT_CRC   0x0ac
#define P210_OP_REG_OUTPUT2_ADDR    0x0dc
#define P210_OP_REG_THRESHOLD       0x0e4

struct P210OperatorState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[P210_OPERATOR_MMIO_SIZE / 4];
};

#endif /* HW_MISC_P210_OPERATOR_H */
