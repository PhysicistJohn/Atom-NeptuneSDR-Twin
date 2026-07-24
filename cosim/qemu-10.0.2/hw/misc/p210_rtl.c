/*
 * P210 RTL co-processor device (QEMU): drives real Verilated RTL in the twin.
 *
 * See include/hw/misc/p210_rtl.h. The device dlopen()s a Verilated block that
 * exposes the rtl_block ABI, then on CONTROL.START it: DMA-reads N interleaved
 * int32 complex samples from INPUT_ADDR, clocks them through the actual RTL
 * (load -> start -> run-until-done -> read), and DMA-writes N results back to
 * OUTPUT_ADDR. The datapath is the Verilog itself, not a C re-model.
 *
 * SPDX-License-Identifier: MIT
 */
#include "qemu/osdep.h"
#include <dlfcn.h>
#include "exec/address-spaces.h"
#include "hw/irq.h"
#include "hw/misc/p210_rtl.h"
#include "hw/sysbus.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "system/dma.h"

/* CONTROL / STATUS bits (shared subset of the v2 ABI). */
#define RTL_CTRL_START      0x00000001u
#define RTL_CTRL_SOFT_RESET 0x00000002u
#define RTL_ST_DONE         0x00000002u
#define RTL_ST_ERROR        0x00000004u

/* Error codes. */
#define RTL_ERR_NONE     0u
#define RTL_ERR_NO_LIB   1u   /* P210_RTL_LIB unset or library failed to load */
#define RTL_ERR_BAD_N    2u   /* requested LOG2_N does not match the block    */
#define RTL_ERR_DMA      3u   /* input DMA read / output DMA write failed     */
#define RTL_ERR_TIMEOUT  4u   /* RTL never asserted done                      */

#define RTL_MAX_CYCLES   4000000ull

static uint32_t idx(hwaddr off) { return (off & 0xfff) >> 2; }

/* Resolve the rtl_block ABI from P210_RTL_LIB; leaves s->block NULL on failure
 * (START then reports RTL_ERR_NO_LIB, so a misconfigured path fails loudly at
 * run time rather than aborting machine creation). */
static void p210_rtl_load_library(P210RtlState *s)
{
    const char *path = getenv("P210_RTL_LIB");
    if (!path || !*path) {
        return;
    }
    s->dl = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!s->dl) {
        qemu_log("p210-rtl: dlopen(%s) failed: %s\n", path, dlerror());
        return;
    }
    s->rtl_open  = dlsym(s->dl, "rtl_open");
    s->rtl_reset = dlsym(s->dl, "rtl_reset");
    s->rtl_load  = dlsym(s->dl, "rtl_load");
    s->rtl_start = dlsym(s->dl, "rtl_start");
    s->rtl_run   = dlsym(s->dl, "rtl_run");
    s->rtl_read  = dlsym(s->dl, "rtl_read");
    s->rtl_close = dlsym(s->dl, "rtl_close");
    s->rtl_log2n = dlsym(s->dl, "rtl_log2n");
    if (!s->rtl_open || !s->rtl_load || !s->rtl_start || !s->rtl_run ||
        !s->rtl_read || !s->rtl_log2n) {
        qemu_log("p210-rtl: %s is missing rtl_block ABI symbols\n", path);
        dlclose(s->dl);
        s->dl = NULL;
        return;
    }
    s->block = s->rtl_open();
}

static void p210_rtl_run(P210RtlState *s)
{
    uint32_t log2n = s->regs[idx(P210_RTL_REG_LOG2_N)];
    uint32_t in_a = s->regs[idx(P210_RTL_REG_INPUT_ADDR)];
    uint32_t out_a = s->regs[idx(P210_RTL_REG_OUTPUT_ADDR)];
    uint32_t n, i;
    int32_t *buf = NULL;

    if (!s->block) {
        s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_NO_LIB;
        s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_ERROR;
        goto done;
    }
    if (log2n != s->rtl_log2n()) {              /* the block is a fixed size */
        s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_BAD_N;
        s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_ERROR;
        goto done;
    }
    n = 1u << log2n;

    buf = g_malloc(n * 8);                       /* n interleaved int32 re,im */
    if (dma_memory_read(&address_space_memory, in_a, buf, n * 8,
                        MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
        s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_DMA;
        s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_ERROR;
        goto done;
    }

    if (s->rtl_reset) {
        s->rtl_reset(s->block);
    }
    for (i = 0; i < n; i++) {
        s->rtl_load(s->block, i, buf[2 * i], buf[2 * i + 1]);
    }
    s->rtl_start(s->block, log2n);
    if (s->rtl_run(s->block, RTL_MAX_CYCLES) != 0) {
        s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_TIMEOUT;
        s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_ERROR;
        goto done;
    }
    for (i = 0; i < n; i++) {
        int32_t re, im;
        s->rtl_read(s->block, i, &re, &im);
        buf[2 * i] = re;
        buf[2 * i + 1] = im;
    }

    if (dma_memory_write(&address_space_memory, out_a, buf, n * 8,
                         MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
        s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_DMA;
        s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_ERROR;
        goto done;
    }
    s->regs[idx(P210_RTL_REG_RESULT_SEQ)]++;
    s->regs[idx(P210_RTL_REG_STATUS)] |= RTL_ST_DONE;
done:
    g_free(buf);
    qemu_set_irq(s->irq, 1);
    qemu_set_irq(s->irq, 0);
}

static uint64_t p210_rtl_read(void *opaque, hwaddr off, unsigned size)
{
    P210RtlState *s = opaque;
    return s->regs[idx(off)];
}

static void p210_rtl_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    P210RtlState *s = opaque;
    uint32_t val = value;
    if (off == P210_RTL_REG_CONTROL) {
        if (val & RTL_CTRL_SOFT_RESET) {
            uint32_t seq = s->regs[idx(P210_RTL_REG_RESULT_SEQ)];
            memset(s->regs, 0, sizeof s->regs);
            s->regs[idx(P210_RTL_REG_RESULT_SEQ)] = seq;
            if (s->block && s->rtl_reset) {
                s->rtl_reset(s->block);
            }
            return;
        }
        if (val & RTL_CTRL_START) {
            s->regs[idx(P210_RTL_REG_STATUS)] &= ~(RTL_ST_DONE | RTL_ST_ERROR);
            s->regs[idx(P210_RTL_REG_ERROR_CODE)] = RTL_ERR_NONE;
            p210_rtl_run(s);
        }
        return;
    }
    s->regs[idx(off)] = val;
}

static const MemoryRegionOps p210_rtl_ops = {
    .read = p210_rtl_read,
    .write = p210_rtl_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
};

static void p210_rtl_reset(DeviceState *dev)
{
    P210RtlState *s = P210_RTL(dev);
    memset(s->regs, 0, sizeof s->regs);
    if (s->block && s->rtl_reset) {
        s->rtl_reset(s->block);
    }
}

static void p210_rtl_init(Object *obj)
{
    P210RtlState *s = P210_RTL(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    memory_region_init_io(&s->iomem, obj, &p210_rtl_ops, s, TYPE_P210_RTL,
                          P210_RTL_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void p210_rtl_realize(DeviceState *dev, Error **errp)
{
    P210RtlState *s = P210_RTL(dev);
    p210_rtl_load_library(s);
}

static void p210_rtl_unrealize(DeviceState *dev)
{
    P210RtlState *s = P210_RTL(dev);
    if (s->block && s->rtl_close) {
        s->rtl_close(s->block);
    }
    if (s->dl) {
        dlclose(s->dl);
    }
    s->block = NULL;
    s->dl = NULL;
}

/* The dlopen'd RTL model holds live simulation state that is not migratable;
 * only the register file is saved. */
static const VMStateDescription vmstate_p210_rtl = {
    .name = TYPE_P210_RTL,
    .version_id = 1, .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, P210RtlState, P210_RTL_MMIO_SIZE / 4),
        VMSTATE_END_OF_LIST()
    }
};

static void p210_rtl_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = p210_rtl_realize;
    dc->unrealize = p210_rtl_unrealize;
    device_class_set_legacy_reset(dc, p210_rtl_reset);
    dc->vmsd = &vmstate_p210_rtl;
}

static const TypeInfo p210_rtl_info = {
    .name = TYPE_P210_RTL,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(P210RtlState),
    .instance_init = p210_rtl_init,
    .class_init = p210_rtl_class_init,
};

static void p210_rtl_register_types(void)
{
    type_register_static(&p210_rtl_info);
}
type_init(p210_rtl_register_types)
