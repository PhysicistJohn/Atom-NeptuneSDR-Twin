/*
 * NeptuneSDR programmable-logic FFT accelerator functional twin.
 *
 * The device gives ARM firmware a stable AXI4-Lite-style register and DDR DMA
 * contract for moving wideband reduction into the Zynq programmable logic.
 * It executes a real deterministic integer radix-2 FFT.  Arithmetic is scaled
 * by one bit at every stage (1/N overall), uses integer CORDIC twiddles, and
 * returns uint32 linear-power bins.  No host floating-point operation affects
 * a result, so snapshots and hosts produce byte-identical output.
 *
 * This software-visible model is not synthesis, resource, CDC, or post-route
 * timing evidence for an XC7Z020 implementation.
 *
 * Copyright (c) 2026 NeptuneSDR Twin contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "exec/address-spaces.h"
#include "hw/irq.h"
#include "hw/misc/p210_fft.h"
#include "hw/sysbus.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "system/dma.h"

#define P210_FFT_CAPABILITIES               (P210_FFT_CAP_IQ16_LE | \
                                             P210_FFT_CAP_POWER_U32_LE | \
                                             P210_FFT_CAP_TWO_CHANNEL | \
                                             P210_FFT_CAP_SCALE_EACH_STAGE | \
                                             P210_FFT_CAP_NATURAL_ORDER | \
                                             P210_FFT_CAP_COMPLETION_IRQ)

#define P210_FFT_BYTES_PER_COMPLEX          4
#define P210_FFT_BYTES_PER_POWER            4
#define P210_FFT_CORDIC_FRAC_BITS           30
#define P210_FFT_CORDIC_GAIN_INVERSE        0x26dd3b6aLL

typedef uint32_t P210FFTError;

typedef struct P210FFTState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[P210_FFT_MMIO_SIZE / sizeof(uint32_t)];
    uint32_t status;
    uint32_t error_code;
    uint32_t result_sequence;
    uint32_t bins_written;
    uint64_t completed_count;
    uint64_t error_count;
} P210FFTState;

DECLARE_INSTANCE_CHECKER(P210FFTState, P210_FFT, TYPE_P210_FFT)

/* atan(2^-i), expressed as unsigned binary turns where one turn is 2^32. */
static const uint32_t p210_fft_cordic_angles[] = {
    0x20000000U, 0x12e4051eU, 0x09fb385bU, 0x051111d4U,
    0x028b0d43U, 0x0145d7e1U, 0x00a2f61eU, 0x00517c55U,
    0x0028be53U, 0x00145f2fU, 0x000a2f98U, 0x000517ccU,
    0x00028be6U, 0x000145f3U, 0x0000a2faU, 0x0000517dU,
    0x000028beU, 0x0000145fU, 0x00000a30U, 0x00000518U,
    0x0000028cU, 0x00000146U, 0x000000a3U, 0x00000051U,
    0x00000029U, 0x00000014U, 0x0000000aU, 0x00000005U,
    0x00000003U, 0x00000001U, 0x00000001U,
};

static int64_t p210_fft_arithmetic_shift(int64_t value, unsigned int bits)
{
    uint64_t magnitude;

    if (value >= 0) {
        return (uint64_t)value >> bits;
    }
    magnitude = (uint64_t)(-value);
    return -(int64_t)((magnitude + (UINT64_C(1) << bits) - 1) >> bits);
}

/* Return cosine and sine in signed Q2.30 for an unsigned binary-turn angle. */
static void p210_fft_cordic(uint32_t angle, int32_t *cosine, int32_t *sine)
{
    const int64_t quarter_turn = INT64_C(0x40000000);
    const int64_t half_turn = INT64_C(0x80000000);
    int64_t z = angle & BIT(31) ?
        (int64_t)angle - INT64_C(0x100000000) : angle;
    int64_t x = P210_FFT_CORDIC_GAIN_INVERSE;
    int64_t y = 0;
    bool negate = false;
    unsigned int i;

    /* CORDIC converges in the first/fourth quadrants. */
    if (z > quarter_turn) {
        z -= half_turn;
        negate = true;
    } else if (z < -quarter_turn) {
        z += half_turn;
        negate = true;
    }

    for (i = 0; i < ARRAY_SIZE(p210_fft_cordic_angles); i++) {
        int64_t shifted_x = p210_fft_arithmetic_shift(x, i);
        int64_t shifted_y = p210_fft_arithmetic_shift(y, i);
        int64_t next_x;
        int64_t next_y;

        if (z >= 0) {
            next_x = x - shifted_y;
            next_y = y + shifted_x;
            z -= p210_fft_cordic_angles[i];
        } else {
            next_x = x + shifted_y;
            next_y = y - shifted_x;
            z += p210_fft_cordic_angles[i];
        }
        x = next_x;
        y = next_y;
    }

    if (negate) {
        x = -x;
        y = -y;
    }
    *cosine = x;
    *sine = y;
}

static int32_t p210_fft_iq16(const uint8_t *bytes)
{
    uint32_t value = bytes[0] | (uint32_t)bytes[1] << 8;

    return value <= INT16_MAX ? value : (int32_t)value - UINT32_C(0x10000);
}

static void p210_fft_store_u32_le(uint8_t *bytes, uint32_t value)
{
    bytes[0] = value;
    bytes[1] = value >> 8;
    bytes[2] = value >> 16;
    bytes[3] = value >> 24;
}

static void p210_fft_bit_reverse(int32_t *real, int32_t *imag, uint32_t count)
{
    uint32_t j = 0;
    uint32_t i;

    for (i = 1; i < count; i++) {
        uint32_t bit = count >> 1;

        while (j & bit) {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if (i < j) {
            int32_t temporary = real[i];

            real[i] = real[j];
            real[j] = temporary;
            temporary = imag[i];
            imag[i] = imag[j];
            imag[j] = temporary;
        }
    }
}

/* In-place forward FFT, natural output order, with one-bit scaling per stage. */
static void p210_fft_radix2(int32_t *real, int32_t *imag, uint32_t count,
                            const int32_t *twiddle_real,
                            const int32_t *twiddle_imag)
{
    uint32_t span;

    p210_fft_bit_reverse(real, imag, count);
    for (span = 2; span <= count; span <<= 1) {
        uint32_t half = span >> 1;
        uint32_t twiddle_stride = count / span;
        uint32_t base;

        for (base = 0; base < count; base += span) {
            uint32_t offset;

            for (offset = 0; offset < half; offset++) {
                uint32_t odd_index = base + offset + half;
                uint32_t twiddle_index = offset * twiddle_stride;
                int64_t odd_real = real[odd_index];
                int64_t odd_imag = imag[odd_index];
                int64_t product_real =
                    (int64_t)twiddle_real[twiddle_index] * odd_real -
                    (int64_t)twiddle_imag[twiddle_index] * odd_imag;
                int64_t product_imag =
                    (int64_t)twiddle_imag[twiddle_index] * odd_real +
                    (int64_t)twiddle_real[twiddle_index] * odd_imag;
                int32_t rotated_real =
                    product_real / (INT64_C(1) << P210_FFT_CORDIC_FRAC_BITS);
                int32_t rotated_imag =
                    product_imag / (INT64_C(1) << P210_FFT_CORDIC_FRAC_BITS);
                int32_t even_real = real[base + offset];
                int32_t even_imag = imag[base + offset];

                real[base + offset] =
                    ((int64_t)even_real + rotated_real) / 2;
                imag[base + offset] =
                    ((int64_t)even_imag + rotated_imag) / 2;
                real[odd_index] =
                    ((int64_t)even_real - rotated_real) / 2;
                imag[odd_index] =
                    ((int64_t)even_imag - rotated_imag) / 2;
            }
        }
    }
}

static void p210_fft_update_irq(P210FFTState *s)
{
    bool pending = s->status & (P210_FFT_STATUS_DONE | P210_FFT_STATUS_ERROR);
    bool enabled = s->regs[P210_FFT_REG_CONTROL / 4] &
        P210_FFT_CONTROL_IRQ_ENABLE;

    qemu_set_irq(s->irq, pending && enabled);
}

static void p210_fft_reset_state(P210FFTState *s)
{
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[P210_FFT_REG_LOG2_N / 4] = P210_FFT_MIN_LOG2_N;
    s->regs[P210_FFT_REG_CHANNEL_COUNT / 4] = P210_FFT_MAX_CHANNELS;
    s->regs[P210_FFT_REG_CHANNEL_MASK / 4] = 0x3;
    s->status = 0;
    s->error_code = P210_FFT_ERROR_NONE;
    s->result_sequence = 0;
    s->bins_written = 0;
    s->completed_count = 0;
    s->error_count = 0;
    p210_fft_update_irq(s);
}

static P210FFTError p210_fft_validate(P210FFTState *s, uint32_t *count,
                                      uint32_t *selected_channels)
{
    uint32_t log2_n = s->regs[P210_FFT_REG_LOG2_N / 4];
    uint32_t channels = s->regs[P210_FFT_REG_CHANNEL_COUNT / 4];
    uint32_t mask = s->regs[P210_FFT_REG_CHANNEL_MASK / 4];
    uint32_t input_address = s->regs[P210_FFT_REG_INPUT_ADDR / 4];
    uint32_t output_address = s->regs[P210_FFT_REG_OUTPUT_ADDR / 4];
    uint32_t input_bytes = s->regs[P210_FFT_REG_INPUT_BYTES / 4];
    uint32_t output_bytes = s->regs[P210_FFT_REG_OUTPUT_BYTES / 4];
    uint64_t expected_input;
    uint64_t expected_output;

    if (log2_n < P210_FFT_MIN_LOG2_N || log2_n > P210_FFT_MAX_LOG2_N) {
        return P210_FFT_ERROR_BAD_LOG2_N;
    }
    *count = UINT32_C(1) << log2_n;
    if (channels < 1 || channels > P210_FFT_MAX_CHANNELS ||
        !mask || (mask & ~((UINT32_C(1) << channels) - 1))) {
        return P210_FFT_ERROR_BAD_CHANNELS;
    }
    *selected_channels = ctpop32(mask);
    if ((input_address | output_address) & 0x3) {
        return P210_FFT_ERROR_BAD_ALIGNMENT;
    }

    expected_input = (uint64_t)*count * channels * P210_FFT_BYTES_PER_COMPLEX;
    expected_output = (uint64_t)*count * *selected_channels *
        P210_FFT_BYTES_PER_POWER;
    if (input_bytes != expected_input || output_bytes != expected_output) {
        return P210_FFT_ERROR_BAD_LENGTH;
    }
    if ((uint64_t)input_address + input_bytes > (UINT64_C(1) << 32) ||
        (uint64_t)output_address + output_bytes > (UINT64_C(1) << 32)) {
        return P210_FFT_ERROR_ADDRESS_RANGE;
    }
    if ((uint64_t)input_address < (uint64_t)output_address + output_bytes &&
        (uint64_t)output_address < (uint64_t)input_address + input_bytes) {
        return P210_FFT_ERROR_BUFFER_OVERLAP;
    }
    return P210_FFT_ERROR_NONE;
}

static P210FFTError p210_fft_transform(P210FFTState *s, uint32_t count,
                                       uint32_t selected_channels)
{
    uint32_t channels = s->regs[P210_FFT_REG_CHANNEL_COUNT / 4];
    uint32_t mask = s->regs[P210_FFT_REG_CHANNEL_MASK / 4];
    uint32_t input_address = s->regs[P210_FFT_REG_INPUT_ADDR / 4];
    uint32_t output_address = s->regs[P210_FFT_REG_OUTPUT_ADDR / 4];
    uint32_t input_bytes = s->regs[P210_FFT_REG_INPUT_BYTES / 4];
    uint32_t output_bytes = s->regs[P210_FFT_REG_OUTPUT_BYTES / 4];
    int32_t *real = NULL;
    int32_t *imag = NULL;
    int32_t *twiddle_real = NULL;
    int32_t *twiddle_imag = NULL;
    uint8_t *input = NULL;
    uint8_t *output = NULL;
    P210FFTError error = P210_FFT_ERROR_ALLOCATION;
    uint32_t channel;
    uint32_t selected = 0;
    uint32_t i;
    MemTxResult result;

    input = g_try_malloc(input_bytes);
    output = g_try_malloc0(output_bytes);
    real = g_try_new(int32_t, count);
    imag = g_try_new(int32_t, count);
    twiddle_real = g_try_new(int32_t, count / 2);
    twiddle_imag = g_try_new(int32_t, count / 2);
    if (!input || !output || !real || !imag ||
        !twiddle_real || !twiddle_imag) {
        goto out;
    }

    result = dma_memory_read(&address_space_memory, input_address, input,
                             input_bytes, MEMTXATTRS_UNSPECIFIED);
    if (result != MEMTX_OK) {
        error = P210_FFT_ERROR_DMA_READ;
        goto out;
    }

    for (i = 0; i < count / 2; i++) {
        uint32_t phase = 0U -
            (uint32_t)(((uint64_t)i << 32) / count);

        p210_fft_cordic(phase, &twiddle_real[i], &twiddle_imag[i]);
    }

    for (channel = 0; channel < channels; channel++) {
        uint32_t sample;

        if (!(mask & BIT(channel))) {
            continue;
        }
        for (sample = 0; sample < count; sample++) {
            const uint8_t *iq = input +
                ((uint64_t)sample * channels + channel) *
                P210_FFT_BYTES_PER_COMPLEX;

            real[sample] = p210_fft_iq16(iq);
            imag[sample] = p210_fft_iq16(iq + 2);
        }
        p210_fft_radix2(real, imag, count, twiddle_real, twiddle_imag);
        for (i = 0; i < count; i++) {
            uint64_t power = (int64_t)real[i] * real[i] +
                (int64_t)imag[i] * imag[i];
            uint32_t encoded = MIN(power, (uint64_t)UINT32_MAX);
            uint8_t *destination = output +
                ((uint64_t)selected * count + i) *
                P210_FFT_BYTES_PER_POWER;

            p210_fft_store_u32_le(destination, encoded);
        }
        selected++;
    }
    g_assert(selected == selected_channels);

    result = dma_memory_write(&address_space_memory, output_address, output,
                              output_bytes, MEMTXATTRS_UNSPECIFIED);
    error = result == MEMTX_OK ? P210_FFT_ERROR_NONE :
        P210_FFT_ERROR_DMA_WRITE;

out:
    g_free(input);
    g_free(output);
    g_free(real);
    g_free(imag);
    g_free(twiddle_real);
    g_free(twiddle_imag);
    return error;
}

static void p210_fft_start(P210FFTState *s)
{
    uint32_t count = 0;
    uint32_t selected_channels = 0;
    P210FFTError error;

    if (s->status & P210_FFT_STATUS_BUSY) {
        s->error_code = P210_FFT_ERROR_BUSY;
        s->status |= P210_FFT_STATUS_ERROR;
        s->error_count++;
        p210_fft_update_irq(s);
        return;
    }

    s->status &= ~(P210_FFT_STATUS_DONE | P210_FFT_STATUS_ERROR);
    s->status |= P210_FFT_STATUS_BUSY;
    s->error_code = P210_FFT_ERROR_NONE;
    s->bins_written = 0;
    p210_fft_update_irq(s);

    error = p210_fft_validate(s, &count, &selected_channels);
    if (error == P210_FFT_ERROR_NONE) {
        error = p210_fft_transform(s, count, selected_channels);
    }

    s->status &= ~P210_FFT_STATUS_BUSY;
    if (error == P210_FFT_ERROR_NONE) {
        s->result_sequence = s->regs[P210_FFT_REG_SEQUENCE / 4];
        s->bins_written = count * selected_channels;
        s->completed_count++;
        s->status |= P210_FFT_STATUS_DONE;
    } else {
        s->error_code = error;
        s->error_count++;
        s->status |= P210_FFT_STATUS_ERROR;
        qemu_log_mask(LOG_GUEST_ERROR,
                      "p210-fft: transform rejected with error %u\n", error);
    }
    p210_fft_update_irq(s);
}

static uint64_t p210_fft_read(void *opaque, hwaddr offset, unsigned int size)
{
    P210FFTState *s = opaque;

    switch (offset) {
    case P210_FFT_REG_ID:
        return P210_FFT_ID;
    case P210_FFT_REG_VERSION:
        return P210_FFT_VERSION;
    case P210_FFT_REG_CAPABILITIES:
        return P210_FFT_CAPABILITIES;
    case P210_FFT_REG_STATUS: {
        uint32_t status = s->status;

        if ((status & (P210_FFT_STATUS_DONE | P210_FFT_STATUS_ERROR)) &&
            (s->regs[P210_FFT_REG_CONTROL / 4] &
             P210_FFT_CONTROL_IRQ_ENABLE)) {
            status |= P210_FFT_STATUS_IRQ_PENDING;
        }
        return status;
    }
    case P210_FFT_REG_ERROR_CODE:
        return s->error_code;
    case P210_FFT_REG_RESULT_SEQUENCE:
        return s->result_sequence;
    case P210_FFT_REG_COMPLETED_LO:
        return s->completed_count;
    case P210_FFT_REG_COMPLETED_HI:
        return s->completed_count >> 32;
    case P210_FFT_REG_ERROR_COUNT_LO:
        return s->error_count;
    case P210_FFT_REG_ERROR_COUNT_HI:
        return s->error_count >> 32;
    case P210_FFT_REG_BINS_WRITTEN:
        return s->bins_written;
    case P210_FFT_REG_MIN_LOG2_N:
        return P210_FFT_MIN_LOG2_N;
    case P210_FFT_REG_MAX_LOG2_N:
        return P210_FFT_MAX_LOG2_N;
    default:
        return s->regs[offset / 4];
    }
}

static void p210_fft_write(void *opaque, hwaddr offset, uint64_t value,
                           unsigned int size)
{
    P210FFTState *s = opaque;
    uint32_t val = value;

    switch (offset) {
    case P210_FFT_REG_CONTROL:
        if (val & P210_FFT_CONTROL_SOFT_RESET) {
            p210_fft_reset_state(s);
            return;
        }
        s->regs[offset / 4] = val & P210_FFT_CONTROL_IRQ_ENABLE;
        p210_fft_update_irq(s);
        if (val & P210_FFT_CONTROL_START) {
            p210_fft_start(s);
        }
        return;
    case P210_FFT_REG_STATUS:
        s->status &= ~(val & (P210_FFT_STATUS_DONE |
                              P210_FFT_STATUS_ERROR));
        p210_fft_update_irq(s);
        return;
    case P210_FFT_REG_LOG2_N:
    case P210_FFT_REG_CHANNEL_COUNT:
    case P210_FFT_REG_CHANNEL_MASK:
    case P210_FFT_REG_INPUT_ADDR:
    case P210_FFT_REG_INPUT_BYTES:
    case P210_FFT_REG_OUTPUT_ADDR:
    case P210_FFT_REG_OUTPUT_BYTES:
    case P210_FFT_REG_SEQUENCE:
        if (!(s->status & P210_FFT_STATUS_BUSY)) {
            s->regs[offset / 4] = val;
        }
        return;
    case P210_FFT_REG_ID:
    case P210_FFT_REG_VERSION:
    case P210_FFT_REG_CAPABILITIES:
    case P210_FFT_REG_ERROR_CODE:
    case P210_FFT_REG_RESULT_SEQUENCE:
    case P210_FFT_REG_COMPLETED_LO:
    case P210_FFT_REG_COMPLETED_HI:
    case P210_FFT_REG_ERROR_COUNT_LO:
    case P210_FFT_REG_ERROR_COUNT_HI:
    case P210_FFT_REG_BINS_WRITTEN:
    case P210_FFT_REG_MIN_LOG2_N:
    case P210_FFT_REG_MAX_LOG2_N:
        return;
    default:
        qemu_log_mask(LOG_GUEST_ERROR,
                      "p210-fft: write to reserved register 0x%" HWADDR_PRIx
                      "\n", offset);
        return;
    }
}

static const MemoryRegionOps p210_fft_ops = {
    .read = p210_fft_read,
    .write = p210_fft_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static void p210_fft_reset(DeviceState *dev)
{
    p210_fft_reset_state(P210_FFT(dev));
}

static int p210_fft_post_load(void *opaque, int version_id)
{
    P210FFTState *s = opaque;

    /* Execution is synchronous, so BUSY is never a migratable boundary. */
    s->status &= ~P210_FFT_STATUS_BUSY;
    p210_fft_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_p210_fft = {
    .name = TYPE_P210_FFT,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = p210_fft_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, P210FFTState,
                             P210_FFT_MMIO_SIZE / sizeof(uint32_t)),
        VMSTATE_UINT32(status, P210FFTState),
        VMSTATE_UINT32(error_code, P210FFTState),
        VMSTATE_UINT32(result_sequence, P210FFTState),
        VMSTATE_UINT32(bins_written, P210FFTState),
        VMSTATE_UINT64(completed_count, P210FFTState),
        VMSTATE_UINT64(error_count, P210FFTState),
        VMSTATE_END_OF_LIST()
    },
};

static void p210_fft_init(Object *obj)
{
    P210FFTState *s = P210_FFT(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &p210_fft_ops, s, TYPE_P210_FFT,
                          P210_FFT_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void p210_fft_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    device_class_set_legacy_reset(dc, p210_fft_reset);
    dc->vmsd = &vmstate_p210_fft;
    dc->desc = "NeptuneSDR programmable-logic FFT accelerator";
}

static const TypeInfo p210_fft_info = {
    .name = TYPE_P210_FFT,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(P210FFTState),
    .instance_init = p210_fft_init,
    .class_init = p210_fft_class_init,
};

static void p210_fft_register_types(void)
{
    type_register_static(&p210_fft_info);
}

type_init(p210_fft_register_types)
