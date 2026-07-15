/*
 * HAMGEEK P210 / NeptuneSDR programmable-logic contacts.
 *
 * The device supplies the register and DMA contacts required by the public
 * P210 device tree: AXI AD9361 ADC/DDS cores and two ADI AXI DMAC instances.
 * RX produces deterministic 2x2 IQ16LE tones in the ADI IIO scan order; TX
 * validates/reads the guest buffer and discards it.  This is sufficient for
 * unmodified driver, firmware and host-stream development, but it is not an RF
 * propagation model.
 *
 * Copyright (c) 2026 NeptuneSDR Twin contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "exec/address-spaces.h"
#include "hw/irq.h"
#include "hw/misc/p210_sdr.h"
#include "hw/qdev-properties.h"
#include "hw/sysbus.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "system/dma.h"

#define P210_RX_CORE_BYTES              0x4000
#define P210_TX_CORE_BYTES              0x1000
#define P210_DMAC_BYTES                 0x10000
#define P210_RX_CORE_VERSION            0x000a0061 /* 10.00.a */
#define P210_TX_CORE_VERSION            0x00090061 /* 9.00.a */
#define P210_DMAC_VERSION               0x00040061 /* 4.00.a */

#define P210_REG_VERSION                0x000
#define P210_REG_ID                     0x004
#define P210_REG_SCRATCH                0x008
#define P210_REG_RSTN                   0x040
#define P210_REG_STATUS                 0x05c
#define P210_REG_DRP_STATUS             0x074
#define P210_REG_DP_DISABLE             0x0c0
#define P210_REG_CHAN_CONTROL(c)        (0x400 + (c) * 0x40)
#define P210_REG_CHAN_STATUS(c)         (0x404 + (c) * 0x40)

#define P210_STATUS_VALID               BIT(0)
#define P210_DRP_LOCKED                 BIT(17)

#define DMAC_REG_IRQ_MASK               0x080
#define DMAC_REG_IRQ_PENDING            0x084
#define DMAC_REG_IRQ_SOURCE             0x088
#define DMAC_REG_CONTROL                0x400
#define DMAC_REG_TRANSFER_ID            0x404
#define DMAC_REG_START_TRANSFER         0x408
#define DMAC_REG_FLAGS                  0x40c
#define DMAC_REG_DEST_ADDRESS           0x410
#define DMAC_REG_SRC_ADDRESS            0x414
#define DMAC_REG_X_LENGTH               0x418
#define DMAC_REG_Y_LENGTH               0x41c
#define DMAC_REG_DEST_STRIDE            0x420
#define DMAC_REG_SRC_STRIDE             0x424
#define DMAC_REG_TRANSFER_DONE          0x428
#define DMAC_REG_ACTIVE_TRANSFER_ID     0x42c
#define DMAC_REG_STATUS                 0x430
#define DMAC_REG_CURRENT_SRC_ADDR       0x434
#define DMAC_REG_CURRENT_DEST_ADDR      0x438

#define DMAC_IRQ_SOT                    BIT(0)
#define DMAC_IRQ_EOT                    BIT(1)
#define DMAC_FLAG_CYCLIC                BIT(0)
#define DMAC_CONTROL_ENABLE             BIT(0)
#define DMAC_X_LENGTH_MASK              0x00ffffff
#define P210_DMA_BYTES_PER_SECOND       245760000ULL
#define P210_DMA_CHUNK                   4096
#define P210_DMAC_QUEUE_DEPTH            4

#define P210_RX_CHANNELS                4
#define P210_RX_TONE_PHASES             64
#define P210_RX_TONE0_AMPLITUDE         1536
#define P210_RX_TONE1_AMPLITUDE         1024
#define P210_RX_TONE0_STEP              5
#define P210_RX_TONE1_STEP              13
#define P210_RX_TONE0_PHASE             0
#define P210_RX_TONE1_PHASE             8
#define P210_AD9361_MAX_CODE             2047

typedef struct P210SDRState P210SDRState;

typedef enum P210RegionKind {
    P210_REGION_RX_CORE,
    P210_REGION_TX_CORE,
    P210_REGION_RX_DMAC,
    P210_REGION_TX_DMAC,
} P210RegionKind;

typedef struct P210Region {
    P210SDRState *parent;
    P210RegionKind kind;
    MemoryRegion iomem;
} P210Region;

typedef struct P210DMAC {
    P210SDRState *parent;
    QEMUTimer *timer;
    uint32_t regs[P210_DMAC_BYTES / sizeof(uint32_t)];
    struct {
        uint32_t id;
        uint32_t flags;
        uint32_t dest_address;
        uint32_t src_address;
        uint32_t x_length;
        uint32_t y_length;
        uint32_t dest_stride;
        uint32_t src_stride;
    } queue[P210_DMAC_QUEUE_DEPTH];
    uint8_t queue_head;
    uint8_t queue_count;
    bool to_memory;
    bool running;
} P210DMAC;

struct P210SDRState {
    SysBusDevice parent_obj;
    P210Region regions[4];
    uint32_t rx_core[P210_RX_CORE_BYTES / sizeof(uint32_t)];
    uint32_t tx_core[P210_TX_CORE_BYTES / sizeof(uint32_t)];
    P210DMAC rx_dmac;
    P210DMAC tx_dmac;
    qemu_irq irq[2];
    uint64_t rx_sample_index;
    uint16_t rx_tone0_amplitude;
    uint16_t rx_tone1_amplitude;
    uint8_t rx_tone0_step;
    uint8_t rx_tone1_step;
    uint8_t rx_tone0_phase;
    uint8_t rx_tone1_phase;
};

DECLARE_INSTANCE_CHECKER(P210SDRState, P210_SDR, TYPE_P210_SDR)

static void p210_dmac_update_irq(P210DMAC *dmac)
{
    uint32_t pending = dmac->regs[DMAC_REG_IRQ_PENDING / 4];
    uint32_t mask = dmac->regs[DMAC_REG_IRQ_MASK / 4];
    uint32_t source = pending & ~mask;
    unsigned int irq = dmac->to_memory ? 0 : 1;

    dmac->regs[DMAC_REG_IRQ_SOURCE / 4] = source;
    qemu_set_irq(dmac->parent->irq[irq], source != 0);
}

static uint64_t p210_dmac_descriptor_length(P210DMAC *dmac,
                                            unsigned int index)
{
    uint64_t x = (uint64_t)dmac->queue[index].x_length + 1;
    uint64_t y = (uint64_t)dmac->queue[index].y_length + 1;

    return x * y;
}

/* One cycle of a signed Q1.15 sine.  The two NCOs use integer LUT steps so a
 * capture whose length is a multiple of 64 lands exactly on FFT bins. */
static const int16_t p210_sine_q15[P210_RX_TONE_PHASES] = {
         0,   3212,   6393,   9512,  12539,  15446,  18204,  20787,
     23170,  25329,  27245,  28898,  30273,  31356,  32137,  32609,
     32767,  32609,  32137,  31356,  30273,  28898,  27245,  25329,
     23170,  20787,  18204,  15446,  12539,   9512,   6393,   3212,
         0,  -3212,  -6393,  -9512, -12539, -15446, -18204, -20787,
    -23170, -25329, -27245, -28898, -30273, -31356, -32137, -32609,
    -32767, -32609, -32137, -31356, -30273, -28898, -27245, -25329,
    -23170, -20787, -18204, -15446, -12539,  -9512,  -6393,  -3212,
};

static uint8_t p210_rx_scan_mask(P210SDRState *s)
{
    uint8_t mask = 0;
    unsigned int channel;

    for (channel = 0; channel < P210_RX_CHANNELS; channel++) {
        if (s->rx_core[P210_REG_CHAN_CONTROL(channel) / 4] & BIT(0)) {
            mask |= BIT(channel);
        }
    }
    return mask;
}

static int16_t p210_rx_tone_sample(P210SDRState *s, unsigned int channel,
                                   uint64_t sample_index)
{
    bool second_rx = channel >= 2;
    bool quadrature = channel & 1;
    uint16_t amplitude = second_rx ? s->rx_tone1_amplitude :
                                     s->rx_tone0_amplitude;
    uint8_t step = second_rx ? s->rx_tone1_step : s->rx_tone0_step;
    uint8_t phase = second_rx ? s->rx_tone1_phase : s->rx_tone0_phase;
    uint8_t lut_index;
    int32_t value;

    amplitude = MIN(amplitude, P210_AD9361_MAX_CODE);
    /* I=cos(theta), Q=sin(theta): the I channel is one quarter-cycle ahead. */
    lut_index = (sample_index * step + phase +
                 (quadrature ? 0 : P210_RX_TONE_PHASES / 4)) &
                (P210_RX_TONE_PHASES - 1);
    value = (int32_t)p210_sine_q15[lut_index] * amplitude;

    return value / 32767;
}

static size_t p210_rx_fill(P210SDRState *s, uint8_t *buffer, size_t count)
{
    uint8_t scan_mask = p210_rx_scan_mask(s);
    unsigned int scan_channels = ctpop8(scan_mask);
    size_t frame_bytes = scan_channels * sizeof(int16_t);
    size_t frames;
    size_t frame;
    size_t pos = 0;
    unsigned int channel;

    if (!frame_bytes) {
        /* A real packer cannot produce data with no enabled scan channels. */
        memset(buffer, 0, count);
        return 0;
    }

    frames = count / frame_bytes;
    for (frame = 0; frame < frames; frame++) {
        uint64_t sample_index = s->rx_sample_index + frame;

        /* ADI scan indices are RX1 I, RX1 Q, RX2 I, RX2 Q.  Each enabled
         * channel is a signed 12-bit value in a 16-bit little-endian slot. */
        for (channel = 0; channel < P210_RX_CHANNELS; channel++) {
            uint16_t value;

            if (!(scan_mask & BIT(channel))) {
                continue;
            }
            value = p210_rx_tone_sample(s, channel, sample_index);
            buffer[pos++] = value;
            buffer[pos++] = value >> 8;
        }
    }
    memset(buffer + pos, 0, count - pos);
    return frames;
}

static void p210_dmac_copy(P210DMAC *dmac, unsigned int index)
{
    uint8_t generated[P210_DMA_CHUNK];
    uint8_t discard[P210_DMA_CHUNK];
    uint64_t remaining = p210_dmac_descriptor_length(dmac, index);
    dma_addr_t address = dmac->to_memory ? dmac->queue[index].dest_address :
                                           dmac->queue[index].src_address;

    while (remaining) {
        size_t count = MIN(remaining, (uint64_t)P210_DMA_CHUNK);
        size_t generated_frames = 0;
        MemTxResult result;

        if (dmac->to_memory) {
            generated_frames = p210_rx_fill(dmac->parent, generated, count);
            result = dma_memory_write(&address_space_memory, address, generated,
                                      count, MEMTXATTRS_UNSPECIFIED);
        } else {
            result = dma_memory_read(&address_space_memory, address, discard,
                                     count, MEMTXATTRS_UNSPECIFIED);
        }
        if (result != MEMTX_OK) {
            qemu_log_mask(LOG_GUEST_ERROR,
                          "p210-sdr: DMA address error at 0x%" HWADDR_PRIx
                          " length 0x%zx\n", address, count);
            break;
        }
        if (dmac->to_memory) {
            dmac->parent->rx_sample_index += generated_frames;
        }
        address += count;
        remaining -= count;
    }

    dmac->regs[(dmac->to_memory ? DMAC_REG_CURRENT_DEST_ADDR :
                DMAC_REG_CURRENT_SRC_ADDR) / 4] = address;
}

static void p210_dmac_schedule_head(P210DMAC *dmac)
{
    unsigned int index;
    uint64_t length;
    uint64_t delay;

    if (dmac->running || !dmac->queue_count) {
        return;
    }
    index = dmac->queue_head;
    length = p210_dmac_descriptor_length(dmac, index);
    dmac->regs[DMAC_REG_ACTIVE_TRANSFER_ID / 4] = dmac->queue[index].id;
    dmac->regs[DMAC_REG_STATUS / 4] = 0;
    dmac->running = true;

    delay = MAX(1ULL, muldiv64(length, NANOSECONDS_PER_SECOND,
                              P210_DMA_BYTES_PER_SECOND));
    timer_mod_ns(dmac->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + delay);
}

static bool p210_dmac_accept_descriptor(P210DMAC *dmac)
{
    unsigned int tail;
    uint32_t id;

    if (!(dmac->regs[DMAC_REG_CONTROL / 4] & DMAC_CONTROL_ENABLE) ||
        dmac->queue_count == P210_DMAC_QUEUE_DEPTH) {
        return false;
    }

    tail = (dmac->queue_head + dmac->queue_count) % P210_DMAC_QUEUE_DEPTH;
    id = dmac->regs[DMAC_REG_TRANSFER_ID / 4] &
         (P210_DMAC_QUEUE_DEPTH - 1);
    dmac->queue[tail].id = id;
    dmac->queue[tail].flags = dmac->regs[DMAC_REG_FLAGS / 4];
    dmac->queue[tail].dest_address =
        dmac->regs[DMAC_REG_DEST_ADDRESS / 4];
    dmac->queue[tail].src_address =
        dmac->regs[DMAC_REG_SRC_ADDRESS / 4];
    dmac->queue[tail].x_length = dmac->regs[DMAC_REG_X_LENGTH / 4];
    dmac->queue[tail].y_length = dmac->regs[DMAC_REG_Y_LENGTH / 4];
    dmac->queue[tail].dest_stride = dmac->regs[DMAC_REG_DEST_STRIDE / 4];
    dmac->queue[tail].src_stride = dmac->regs[DMAC_REG_SRC_STRIDE / 4];
    dmac->queue_count++;

    /* The four hardware IDs are also the four TRANSFER_DONE bitmap bits. */
    dmac->regs[DMAC_REG_TRANSFER_DONE / 4] &= ~BIT(id);
    dmac->regs[DMAC_REG_TRANSFER_ID / 4] =
        (id + 1) & (P210_DMAC_QUEUE_DEPTH - 1);
    dmac->regs[DMAC_REG_START_TRANSFER / 4] = 0;
    if (!(dmac->queue[tail].flags & DMAC_FLAG_CYCLIC)) {
        dmac->regs[DMAC_REG_IRQ_PENDING / 4] |= DMAC_IRQ_SOT;
    }
    p210_dmac_update_irq(dmac);
    p210_dmac_schedule_head(dmac);
    return true;
}

static void p210_dmac_complete(void *opaque)
{
    P210DMAC *dmac = opaque;
    unsigned int index = dmac->queue_head;
    uint32_t id_bit;
    uint32_t flags;

    if (!dmac->running || !dmac->queue_count) {
        return;
    }
    id_bit = BIT(dmac->queue[index].id);
    flags = dmac->queue[index].flags;

    p210_dmac_copy(dmac, index);

    if (flags & DMAC_FLAG_CYCLIC) {
        uint64_t delay = MAX(1ULL,
            muldiv64(p210_dmac_descriptor_length(dmac, index),
                     NANOSECONDS_PER_SECOND,
                     P210_DMA_BYTES_PER_SECOND));
        timer_mod_ns(dmac->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + delay);
        return;
    }

    dmac->regs[DMAC_REG_TRANSFER_DONE / 4] |= id_bit;
    dmac->regs[DMAC_REG_IRQ_PENDING / 4] |= DMAC_IRQ_EOT;
    dmac->queue_head = (dmac->queue_head + 1) % P210_DMAC_QUEUE_DEPTH;
    dmac->queue_count--;
    dmac->running = false;
    if (dmac->queue_count) {
        p210_dmac_schedule_head(dmac);
    } else {
        dmac->regs[DMAC_REG_ACTIVE_TRANSFER_ID / 4] =
            dmac->regs[DMAC_REG_TRANSFER_ID / 4];
    }

    /* A submit held while the four-entry queue was full is accepted as soon
     * as the completed descriptor releases a slot. */
    if (dmac->regs[DMAC_REG_START_TRANSFER / 4] & 1) {
        p210_dmac_accept_descriptor(dmac);
    }
    p210_dmac_update_irq(dmac);
}

static uint64_t p210_core_read(void *opaque, hwaddr offset, unsigned size)
{
    P210Region *region = opaque;
    uint32_t *regs = region->kind == P210_REGION_RX_CORE ?
        region->parent->rx_core : region->parent->tx_core;

    if (region->kind == P210_REGION_RX_CORE &&
        offset == P210_REG_STATUS) {
        /* The AD9361 digital-interface tune requires a valid receive clock. */
        return regs[offset / 4] | P210_STATUS_VALID;
    }

    return regs[offset / 4];
}

static void p210_core_write(void *opaque, hwaddr offset, uint64_t value,
                            unsigned size)
{
    P210Region *region = opaque;
    uint32_t *regs = region->kind == P210_REGION_RX_CORE ?
        region->parent->rx_core : region->parent->tx_core;

    if (offset == P210_REG_VERSION || offset == P210_REG_ID ||
        offset == P210_REG_DP_DISABLE) {
        return;
    }

    if (region->kind == P210_REGION_RX_CORE &&
        offset >= P210_REG_CHAN_STATUS(0) &&
        offset <= P210_REG_CHAN_STATUS(15) &&
        (offset - P210_REG_CHAN_STATUS(0)) % 0x40 == 0) {
        /* PN_ERR/PN_OOS/OVER_RANGE are write-one-to-clear in the HDL core. */
        regs[offset / 4] &= ~(uint32_t)value;
        return;
    }
    regs[offset / 4] = value;
}

static P210DMAC *p210_region_dmac(P210Region *region)
{
    return region->kind == P210_REGION_RX_DMAC ?
        &region->parent->rx_dmac : &region->parent->tx_dmac;
}

static uint64_t p210_dmac_read(void *opaque, hwaddr offset, unsigned size)
{
    P210DMAC *dmac = p210_region_dmac(opaque);

    return dmac->regs[offset / 4];
}

static void p210_dmac_write(void *opaque, hwaddr offset, uint64_t value,
                            unsigned size)
{
    P210DMAC *dmac = p210_region_dmac(opaque);
    uint32_t val = value;

    switch (offset) {
    case DMAC_REG_IRQ_PENDING:
        dmac->regs[offset / 4] &= ~val;
        p210_dmac_update_irq(dmac);
        return;
    case DMAC_REG_IRQ_SOURCE:
    case DMAC_REG_TRANSFER_ID:
    case DMAC_REG_TRANSFER_DONE:
    case DMAC_REG_ACTIVE_TRANSFER_ID:
    case DMAC_REG_STATUS:
    case DMAC_REG_CURRENT_SRC_ADDR:
    case DMAC_REG_CURRENT_DEST_ADDR:
        return;
    case DMAC_REG_CONTROL:
        dmac->regs[offset / 4] = val & DMAC_CONTROL_ENABLE;
        if (!(val & DMAC_CONTROL_ENABLE)) {
            timer_del(dmac->timer);
            memset(dmac->queue, 0, sizeof(dmac->queue));
            dmac->queue_head = 0;
            dmac->queue_count = 0;
            dmac->running = false;
            dmac->regs[DMAC_REG_TRANSFER_ID / 4] = 0;
            dmac->regs[DMAC_REG_START_TRANSFER / 4] = 0;
            dmac->regs[DMAC_REG_TRANSFER_DONE / 4] = 0;
            dmac->regs[DMAC_REG_ACTIVE_TRANSFER_ID / 4] = 0;
        }
        return;
    case DMAC_REG_START_TRANSFER:
        if ((val & 1) && dmac->regs[offset / 4] == 0) {
            dmac->regs[offset / 4] = 1;
            p210_dmac_accept_descriptor(dmac);
        }
        return;
    case DMAC_REG_X_LENGTH:
        /* DMA_LENGTH_WIDTH is 24 in the ADI reference core.  Capability
         * detection depends on the upper bits reading back as zero after an
         * all-ones write; returning UINT_MAX makes the 4.14 driver's
         * DIV_ROUND_UP() overflow and eventually divide by zero. */
        dmac->regs[offset / 4] = val & DMAC_X_LENGTH_MASK;
        return;
    default:
        dmac->regs[offset / 4] = val;
        if (offset == DMAC_REG_IRQ_MASK) {
            p210_dmac_update_irq(dmac);
        }
        return;
    }
}

static const MemoryRegionOps p210_core_ops = {
    .read = p210_core_read,
    .write = p210_core_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static const MemoryRegionOps p210_dmac_ops = {
    .read = p210_dmac_read,
    .write = p210_dmac_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static void p210_sdr_reset(DeviceState *dev)
{
    P210SDRState *s = P210_SDR(dev);

    timer_del(s->rx_dmac.timer);
    timer_del(s->tx_dmac.timer);
    memset(s->rx_core, 0, sizeof(s->rx_core));
    memset(s->tx_core, 0, sizeof(s->tx_core));
    memset(s->rx_dmac.regs, 0, sizeof(s->rx_dmac.regs));
    memset(s->tx_dmac.regs, 0, sizeof(s->tx_dmac.regs));
    memset(s->rx_dmac.queue, 0, sizeof(s->rx_dmac.queue));
    memset(s->tx_dmac.queue, 0, sizeof(s->tx_dmac.queue));
    s->rx_core[P210_REG_VERSION / 4] = P210_RX_CORE_VERSION;
    s->tx_core[P210_REG_VERSION / 4] = P210_TX_CORE_VERSION;
    s->rx_core[P210_REG_ID / 4] = 0;
    s->tx_core[P210_REG_ID / 4] = 0;
    s->rx_core[P210_REG_STATUS / 4] = P210_STATUS_VALID;
    s->tx_core[P210_REG_DRP_STATUS / 4] = P210_DRP_LOCKED;
    s->rx_dmac.regs[P210_REG_VERSION / 4] = P210_DMAC_VERSION;
    s->tx_dmac.regs[P210_REG_VERSION / 4] = P210_DMAC_VERSION;
    s->rx_dmac.running = false;
    s->tx_dmac.running = false;
    s->rx_dmac.queue_head = 0;
    s->rx_dmac.queue_count = 0;
    s->tx_dmac.queue_head = 0;
    s->tx_dmac.queue_count = 0;
    s->rx_sample_index = 0;
    qemu_set_irq(s->irq[0], 0);
    qemu_set_irq(s->irq[1], 0);
}

static void p210_sdr_init(Object *obj)
{
    P210SDRState *s = P210_SDR(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    static const char *names[] = {
        "p210-rx-core", "p210-tx-core", "p210-rx-dmac", "p210-tx-dmac"
    };
    static const uint64_t sizes[] = {
        P210_RX_CORE_BYTES, P210_TX_CORE_BYTES, P210_DMAC_BYTES, P210_DMAC_BYTES
    };
    unsigned int i;

    for (i = 0; i < ARRAY_SIZE(s->regions); i++) {
        P210Region *region = &s->regions[i];
        const MemoryRegionOps *ops = i < 2 ? &p210_core_ops : &p210_dmac_ops;

        region->parent = s;
        region->kind = i;
        memory_region_init_io(&region->iomem, obj, ops, region, names[i], sizes[i]);
        sysbus_init_mmio(sbd, &region->iomem);
    }
    sysbus_init_irq(sbd, &s->irq[0]);
    sysbus_init_irq(sbd, &s->irq[1]);

    s->rx_dmac.parent = s;
    s->rx_dmac.to_memory = true;
    s->rx_dmac.timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, p210_dmac_complete,
                                    &s->rx_dmac);
    s->tx_dmac.parent = s;
    s->tx_dmac.to_memory = false;
    s->tx_dmac.timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, p210_dmac_complete,
                                    &s->tx_dmac);
}

static void p210_sdr_finalize(Object *obj)
{
    P210SDRState *s = P210_SDR(obj);

    timer_free(s->rx_dmac.timer);
    timer_free(s->tx_dmac.timer);
}

static const VMStateDescription vmstate_p210_sdr = {
    .name = TYPE_P210_SDR,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(rx_core, P210SDRState,
                             P210_RX_CORE_BYTES / sizeof(uint32_t)),
        VMSTATE_UINT32_ARRAY(tx_core, P210SDRState,
                             P210_TX_CORE_BYTES / sizeof(uint32_t)),
        VMSTATE_UINT32_ARRAY(rx_dmac.regs, P210SDRState,
                             P210_DMAC_BYTES / sizeof(uint32_t)),
        VMSTATE_UINT32_ARRAY(tx_dmac.regs, P210SDRState,
                             P210_DMAC_BYTES / sizeof(uint32_t)),
        VMSTATE_UINT64(rx_sample_index, P210SDRState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property p210_sdr_properties[] = {
    DEFINE_PROP_UINT16("rx-tone0-amplitude", P210SDRState,
                       rx_tone0_amplitude, P210_RX_TONE0_AMPLITUDE),
    DEFINE_PROP_UINT16("rx-tone1-amplitude", P210SDRState,
                       rx_tone1_amplitude, P210_RX_TONE1_AMPLITUDE),
    DEFINE_PROP_UINT8("rx-tone0-step", P210SDRState,
                      rx_tone0_step, P210_RX_TONE0_STEP),
    DEFINE_PROP_UINT8("rx-tone1-step", P210SDRState,
                      rx_tone1_step, P210_RX_TONE1_STEP),
    DEFINE_PROP_UINT8("rx-tone0-phase", P210SDRState,
                      rx_tone0_phase, P210_RX_TONE0_PHASE),
    DEFINE_PROP_UINT8("rx-tone1-phase", P210SDRState,
                      rx_tone1_phase, P210_RX_TONE1_PHASE),
};

static void p210_sdr_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    device_class_set_legacy_reset(dc, p210_sdr_reset);
    device_class_set_props(dc, p210_sdr_properties);
    dc->vmsd = &vmstate_p210_sdr;
    dc->desc = "HAMGEEK P210 SDR programmable logic";
}

static const TypeInfo p210_sdr_info = {
    .name = TYPE_P210_SDR,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(P210SDRState),
    .instance_init = p210_sdr_init,
    .instance_finalize = p210_sdr_finalize,
    .class_init = p210_sdr_class_init,
};

static void p210_sdr_register_types(void)
{
    type_register_static(&p210_sdr_info);
}

type_init(p210_sdr_register_types)
