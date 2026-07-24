/*
 * P210 RTL co-processor device (QEMU): runs your real Verilog in the twin.
 *
 * Unlike p210-operator (a C transliteration of the golden arithmetic), this
 * device does not implement the datapath itself. At realize it dlopen()s a
 * Verilated RTL block -- an actual compiled-from-Verilog model exposing the
 * rtl_block ABI (see cosim/rtl-cosim/rtl_block.h) -- and on START it drives that
 * RTL cycle-by-cycle over the machine's MMIO/DMA. The bundled example block is
 * cosim/operator-rtl/p210_fft_synth.v (the synthesizable, timing-closed FFT
 * engine); swap in your own .v, re-Verilate, and it executes in the emulated
 * Zynq with no QEMU rebuild.
 *
 * The library path comes from the P210_RTL_LIB environment variable so the RTL
 * can change without touching QEMU. This is genuine RTL co-simulation inside the
 * twin, not a functional re-model.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef HW_MISC_P210_RTL_H
#define HW_MISC_P210_RTL_H

#include "hw/sysbus.h"
#include "qom/object.h"

#define TYPE_P210_RTL "p210-rtl"
OBJECT_DECLARE_SIMPLE_TYPE(P210RtlState, P210_RTL)

#define P210_RTL_MMIO_SIZE 0x1000

/* Register offsets (shared subset of the v2 ABI, so one driver drives either
 * the operator or an RTL block). */
#define P210_RTL_REG_CONTROL      0x00c
#define P210_RTL_REG_STATUS       0x010
#define P210_RTL_REG_ERROR_CODE   0x014
#define P210_RTL_REG_LOG2_N       0x018
#define P210_RTL_REG_INPUT_ADDR   0x024
#define P210_RTL_REG_RESULT_SEQ   0x038
#define P210_RTL_REG_OUTPUT_ADDR  0x0dc

struct P210RtlState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[P210_RTL_MMIO_SIZE / 4];

    void *dl;         /* dlopen handle for the Verilated block library */
    void *block;      /* rtl_block * returned by rtl_open()            */
    /* rtl_block ABI entry points resolved from the library. */
    void *(*rtl_open)(void);
    void  (*rtl_reset)(void *b);
    void  (*rtl_load)(void *b, uint32_t idx, int32_t re, int32_t im);
    void  (*rtl_start)(void *b, uint32_t log2n);
    int   (*rtl_run)(void *b, uint64_t max_cycles);
    void  (*rtl_read)(void *b, uint32_t idx, int32_t *re, int32_t *im);
    void  (*rtl_close)(void *b);
    uint32_t (*rtl_log2n)(void);
};

#endif /* HW_MISC_P210_RTL_H */
