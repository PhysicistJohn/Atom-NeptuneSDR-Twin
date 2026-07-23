/*
 * P210 v2 spectral neural-operator PL device (QEMU).
 *
 * Executes golden-arithmetic v1 through the v2 register/DMA ABI: a
 * single-channel diagonal operator FFT -> spectral multiply -> IFFT -> modReLU,
 * bit-exact to the golden reference (Atom-Neural-RL golden.py). The arithmetic
 * is a direct transliteration of that reference; twiddles are the committed
 * 18-bit ROM. Functional QEMU model, not synthesized RTL.
 *
 * SPDX-License-Identifier: MIT
 */
#include "qemu/osdep.h"
#include "exec/address-spaces.h"
#include "hw/irq.h"
#include "hw/misc/p210_operator.h"
#include "hw/sysbus.h"
#include "migration/vmstate.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "system/dma.h"
#include "p210_twiddle_rom.h"

#define TWIDDLE_FRAC 17
#define TABLE_FRAC   15
#define DATA_MAX     ((int64_t)8388607)
#define DATA_MIN     ((int64_t)-8388608)

/* CONTROL / STATUS bits. */
#define OP_CTRL_START      0x00000001u
#define OP_CTRL_SOFT_RESET 0x00000002u
#define OP_CTRL_BYPASS     0x00000200u
#define OP_CTRL_ACTIVATE   0x00000400u
#define OP_ST_DONE         0x00000002u
#define OP_ST_ERROR        0x00000004u
#define OP_ST_WT_READY     0x00000010u
#define OP_ST_BANK_CRC_FAIL 0x00000020u
#define OP_ERR_NONE        0u
#define OP_ERR_BANK_INTEGRITY 11u
#define OP_ERR_BAD_OP_CONFIG  12u

/* --- golden arithmetic (transliterated from golden.py) --- */
static int64_t rhe(int64_t v, unsigned s)
{
    int64_t half, mask, q;
    if (s == 0) {
        return v;
    }
    half = (int64_t)1 << (s - 1);
    mask = ((int64_t)1 << s) - 1;
    q = (v + half) >> s;
    if ((v & mask) == half && (q & 1)) {
        q -= 1;
    }
    return q;
}
static int64_t clamp24(int64_t v)
{
    return v > DATA_MAX ? DATA_MAX : (v < DATA_MIN ? DATA_MIN : v);
}
static void twiddle(unsigned n, unsigned k, int64_t *wr, int64_t *wi)
{
    unsigned idx = k * (65536u / n);
    *wr = p210_twiddle_rom[2 * idx];
    *wi = -(int64_t)p210_twiddle_rom[2 * idx + 1];
}
static unsigned bitrev(unsigned v, unsigned bits)
{
    unsigned r = 0, b;
    for (b = 0; b < bits; b++) {
        r |= ((v >> b) & 1u) << (bits - 1 - b);
    }
    return r;
}
static void gfft(int64_t *re, int64_t *im, unsigned n)
{
    unsigned bits = 0, s, start, j, i;
    while ((1u << bits) < n) {
        bits++;
    }
    for (i = 0; i < n; i++) {
        unsigned jr = bitrev(i, bits);
        if (jr > i) {
            int64_t t;
            t = re[i]; re[i] = re[jr]; re[jr] = t;
            t = im[i]; im[i] = im[jr]; im[jr] = t;
        }
    }
    for (s = 0; s < bits; s++) {
        unsigned half = 1u << s, step = half << 1;
        for (start = 0; start < n; start += step) {
            for (j = 0; j < half; j++) {
                int64_t wr, wi, tr, ti, ar, ai;
                unsigned ia = start + j, ib = ia + half;
                twiddle(n, j * (n / step), &wr, &wi);
                tr = rhe(re[ib] * wr - im[ib] * wi, TWIDDLE_FRAC);
                ti = rhe(re[ib] * wi + im[ib] * wr, TWIDDLE_FRAC);
                ar = re[ia]; ai = im[ia];
                re[ia] = rhe(ar + tr, 1); im[ia] = rhe(ai + ti, 1);
                re[ib] = rhe(ar - tr, 1); im[ib] = rhe(ai - ti, 1);
            }
        }
    }
    for (i = 0; i < n; i++) {
        re[i] = clamp24(re[i]); im[i] = clamp24(im[i]);
    }
}
static void gifft(int64_t *re, int64_t *im, unsigned n)
{
    unsigned i;
    for (i = 0; i < n; i++) {
        im[i] = -im[i];
    }
    gfft(re, im, n);
    for (i = 0; i < n; i++) {
        im[i] = -im[i];
    }
}
static void gmodrelu(int64_t *zr, int64_t *zi, unsigned n, int64_t b_q23, int be)
{
    int64_t b_eff;
    unsigned i;
    if (be > 0) {
        b_eff = rhe(b_q23, (unsigned)be);
    } else if (be < 0) {
        b_eff = b_q23 << (unsigned)(-be);
    } else {
        b_eff = b_q23;
    }
    for (i = 0; i < n; i++) {
        int64_t ar = zr[i] < 0 ? -zr[i] : zr[i];
        int64_t ai = zi[i] < 0 ? -zi[i] : zi[i];
        int64_t mx = ar > ai ? ar : ai, mn = ar > ai ? ai : ar;
        int64_t mag = rhe(15 * mx, 4) + rhe(15 * mn, 5);
        int64_t keep = mag + b_eff, scale = 0;
        if (keep > 0 && mag > 0) {
            scale = (keep << TABLE_FRAC) / mag;
        }
        zr[i] = clamp24(rhe(zr[i] * scale, TABLE_FRAC));
        zi[i] = clamp24(rhe(zi[i] * scale, TABLE_FRAC));
    }
}
/* CRC32 (IEEE, reflected) -- matches zlib crc32 used by the bank compiler. */
static uint32_t op_crc32(const uint8_t *p, size_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    size_t i; int k;
    for (i = 0; i < n; i++) {
        c ^= p[i];
        for (k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (-(int32_t)(c & 1)));
        }
    }
    return c ^ 0xFFFFFFFFu;
}

/* --- device --- */
static uint32_t idx(hwaddr off) { return (off & 0xfff) >> 2; }

static void p210_op_run(P210OperatorState *s)
{
    uint32_t n = 1u << s->regs[idx(P210_OP_REG_LOG2_N)];
    uint32_t in_a = s->regs[idx(P210_OP_REG_INPUT_ADDR)];
    uint32_t out_a = s->regs[idx(P210_OP_REG_OUTPUT2_ADDR)];
    uint32_t log2n = s->regs[idx(P210_OP_REG_LOG2_N)];
    int32_t b_q23 = (int32_t)s->regs[idx(P210_OP_REG_THRESHOLD)];
    int32_t *in = NULL, *out = NULL;
    uint8_t *blob = NULL;
    int64_t *re = NULL, *im = NULL;
    int16_t *hr = NULL, *hi = NULL;
    uint32_t modes, i, wbytes, waddr, wcrc;

    if (s->regs[idx(P210_OP_REG_OP_FLAGS)] & 1u) {         /* BYPASS: passthrough */
        void *buf = g_malloc(n * 8);
        if (dma_memory_read(&address_space_memory, in_a, buf, n * 8, MEMTXATTRS_UNSPECIFIED) == MEMTX_OK) {
            dma_memory_write(&address_space_memory, out_a, buf, n * 8, MEMTXATTRS_UNSPECIFIED);
        }
        g_free(buf);
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_DONE;
        return;
    }

    waddr = s->regs[idx(P210_OP_REG_WEIGHT_ADDR)];
    wbytes = s->regs[idx(P210_OP_REG_WEIGHT_BYTES)];
    wcrc = s->regs[idx(P210_OP_REG_WEIGHT_CRC)];
    blob = g_malloc(wbytes);
    if (dma_memory_read(&address_space_memory, waddr, blob, wbytes, MEMTXATTRS_UNSPECIFIED) != MEMTX_OK ||
        op_crc32(blob, wbytes) != wcrc) {
        s->regs[idx(P210_OP_REG_ERROR_CODE)] = OP_ERR_BANK_INTEGRITY;
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_ERROR | OP_ST_BANK_CRC_FAIL;
        goto out;
    }
    memcpy(&modes, blob, 4);
    if (modes != n) {
        s->regs[idx(P210_OP_REG_ERROR_CODE)] = OP_ERR_BAD_OP_CONFIG;
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_ERROR;
        goto out;
    }
    hr = (int16_t *)(blob + 4);
    hi = (int16_t *)(blob + 4 + modes * 2);

    in = g_malloc(n * 8);
    out = g_malloc(n * 8);
    re = g_new(int64_t, n);
    im = g_new(int64_t, n);
    if (dma_memory_read(&address_space_memory, in_a, in, n * 8, MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_ERROR;
        goto out;
    }
    for (i = 0; i < n; i++) { re[i] = in[2*i]; im[i] = in[2*i+1]; }
    gfft(re, im, n);
    for (i = 0; i < n; i++) {                              /* spectral multiply Q1.15 */
        int64_t xr = re[i], xi = im[i];
        re[i] = clamp24(rhe(xr * hr[i] - xi * hi[i], TABLE_FRAC));
        im[i] = clamp24(rhe(xr * hi[i] + xi * hr[i], TABLE_FRAC));
    }
    gifft(re, im, n);
    gmodrelu(re, im, n, b_q23, (int)log2n);
    for (i = 0; i < n; i++) { out[2*i] = (int32_t)re[i]; out[2*i+1] = (int32_t)im[i]; }
    dma_memory_write(&address_space_memory, out_a, out, n * 8, MEMTXATTRS_UNSPECIFIED);

    s->regs[idx(P210_OP_REG_RESULT_SEQUENCE)]++;
    s->regs[idx(P210_OP_REG_RESULT_WT_CRC)] = s->regs[idx(P210_OP_REG_ACTIVE_WT_CRC)];
    s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_DONE;
out:
    g_free(blob); g_free(in); g_free(out); g_free(re); g_free(im);
}

static void p210_op_activate(P210OperatorState *s)
{
    uint32_t waddr = s->regs[idx(P210_OP_REG_WEIGHT_ADDR)];
    uint32_t wbytes = s->regs[idx(P210_OP_REG_WEIGHT_BYTES)];
    uint32_t want = s->regs[idx(P210_OP_REG_WEIGHT_CRC)];
    uint8_t *blob = g_malloc(wbytes);
    uint32_t got;
    if (dma_memory_read(&address_space_memory, waddr, blob, wbytes, MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
        s->regs[idx(P210_OP_REG_ERROR_CODE)] = OP_ERR_BANK_INTEGRITY;
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_BANK_CRC_FAIL;
        g_free(blob);
        return;
    }
    got = op_crc32(blob, wbytes);
    g_free(blob);
    if (got != want) {
        s->regs[idx(P210_OP_REG_ERROR_CODE)] = OP_ERR_BANK_INTEGRITY;
        s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_BANK_CRC_FAIL;
        return;
    }
    s->regs[idx(P210_OP_REG_ACTIVE_WT_CRC)] = got;
    s->regs[idx(P210_OP_REG_STATUS)] &= ~OP_ST_BANK_CRC_FAIL;
    s->regs[idx(P210_OP_REG_STATUS)] |= OP_ST_WT_READY;
}

static uint64_t p210_op_read(void *opaque, hwaddr off, unsigned size)
{
    P210OperatorState *s = opaque;
    return s->regs[idx(off)];
}

static void p210_op_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    P210OperatorState *s = opaque;
    uint32_t val = value;
    if (off == P210_OP_REG_CONTROL) {
        if (val & OP_CTRL_SOFT_RESET) { memset(s->regs, 0, sizeof s->regs); s->regs[idx(P210_OP_REG_OP_FLAGS)] = 1; return; }
        if (val & OP_CTRL_BYPASS)   { s->regs[idx(P210_OP_REG_OP_FLAGS)] |= 1u; }
        if (val & OP_CTRL_ACTIVATE) { p210_op_activate(s); }
        if (val & OP_CTRL_START)    { s->regs[idx(P210_OP_REG_STATUS)] &= ~(OP_ST_DONE | OP_ST_ERROR); p210_op_run(s); }
        return;
    }
    s->regs[idx(off)] = val;
}

static const MemoryRegionOps p210_op_ops = {
    .read = p210_op_read,
    .write = p210_op_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
};

static void p210_op_reset(DeviceState *dev)
{
    P210OperatorState *s = P210_OPERATOR(dev);
    memset(s->regs, 0, sizeof s->regs);
    s->regs[idx(P210_OP_REG_OP_FLAGS)] = 1;   /* reset = bypass */
}

static void p210_op_init(Object *obj)
{
    P210OperatorState *s = P210_OPERATOR(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);
    memory_region_init_io(&s->iomem, obj, &p210_op_ops, s, TYPE_P210_OPERATOR, P210_OPERATOR_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static const VMStateDescription vmstate_p210_op = {
    .name = TYPE_P210_OPERATOR,
    .version_id = 1, .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, P210OperatorState, P210_OPERATOR_MMIO_SIZE / 4),
        VMSTATE_END_OF_LIST()
    }
};

static void p210_op_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    device_class_set_legacy_reset(dc, p210_op_reset);
    dc->vmsd = &vmstate_p210_op;
}

static const TypeInfo p210_op_info = {
    .name = TYPE_P210_OPERATOR,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(P210OperatorState),
    .instance_init = p210_op_init,
    .class_init = p210_op_class_init,
};

static void p210_op_register_types(void)
{
    type_register_static(&p210_op_info);
}
type_init(p210_op_register_types)
