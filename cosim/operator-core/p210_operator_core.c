/*
 * Golden-arithmetic v1 compute core -- line-for-line with golden.py.
 * SPDX-License-Identifier: MIT
 */
#include "p210_operator_core.h"

static const int32_t *g_rom = 0;
static size_t g_rom_words = 0;

int64_t p210_op_rhe(int64_t v, unsigned s)
{
    if (s == 0) {
        return v;
    }
    int64_t half = (int64_t)1 << (s - 1);
    int64_t mask = ((int64_t)1 << s) - 1;
    int64_t q = (v + half) >> s;
    if ((v & mask) == half && (q & 1)) {
        q -= 1;
    }
    return q;
}

int64_t p210_op_clamp24(int64_t v)
{
    if (v > P210_OP_DATA_MAX) {
        return P210_OP_DATA_MAX;
    }
    if (v < P210_OP_DATA_MIN) {
        return P210_OP_DATA_MIN;
    }
    return v;
}

int p210_op_set_rom(const int32_t *rom, size_t words)
{
    if (!rom || words != (size_t)P210_OP_ROM_HALF_TURN * 2) {
        return -1;
    }
    g_rom = rom;
    g_rom_words = words;
    return 0;
}

static void twiddle_for(unsigned n, unsigned k, int64_t *wr, int64_t *wi)
{
    unsigned stride = P210_OP_ROM_RESOLUTION / n;
    unsigned idx = k * stride;
    *wr = (int64_t)g_rom[2 * idx];
    *wi = -(int64_t)g_rom[2 * idx + 1]; /* e^{-j theta} */
}

static unsigned bit_reverse(unsigned v, unsigned bits)
{
    unsigned r = 0;
    for (unsigned b = 0; b < bits; b++) {
        r |= ((v >> b) & 1u) << (bits - 1 - b);
    }
    return r;
}

int p210_op_fft(int64_t *re, int64_t *im, unsigned n)
{
    if (!g_rom || n == 0 || (n & (n - 1)) || n > 65536) {
        return -1;
    }
    unsigned bits = 0;
    while ((1u << bits) < n) {
        bits++;
    }
    /* bit-reversal permutation (swap once per pair) */
    for (unsigned i = 0; i < n; i++) {
        unsigned j = bit_reverse(i, bits);
        if (j > i) {
            int64_t t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (unsigned s = 0; s < bits; s++) {
        unsigned half = 1u << s;
        unsigned step = half << 1;
        for (unsigned start = 0; start < n; start += step) {
            for (unsigned j = 0; j < half; j++) {
                int64_t wr, wi;
                twiddle_for(n, j * (n / step), &wr, &wi);
                unsigned ia = start + j;
                unsigned ib = ia + half;
                int64_t br = re[ib], bi = im[ib];
                int64_t tr = p210_op_rhe(br * wr - bi * wi, P210_OP_TWIDDLE_FRAC);
                int64_t ti = p210_op_rhe(br * wi + bi * wr, P210_OP_TWIDDLE_FRAC);
                int64_t ar = re[ia], ai = im[ia];
                re[ia] = p210_op_rhe(ar + tr, 1);
                im[ia] = p210_op_rhe(ai + ti, 1);
                re[ib] = p210_op_rhe(ar - tr, 1);
                im[ib] = p210_op_rhe(ai - ti, 1);
            }
        }
    }
    for (unsigned i = 0; i < n; i++) {
        re[i] = p210_op_clamp24(re[i]);
        im[i] = p210_op_clamp24(im[i]);
    }
    return 0;
}

int p210_op_ifft(int64_t *re, int64_t *im, unsigned n)
{
    for (unsigned i = 0; i < n; i++) {
        im[i] = -im[i];
    }
    int rc = p210_op_fft(re, im, n);
    for (unsigned i = 0; i < n; i++) {
        im[i] = -im[i];
    }
    return rc;
}

void p210_op_spectral_multiply(int64_t *xr, int64_t *xi,
                               const int16_t *hr, const int16_t *hi, unsigned n)
{
    for (unsigned i = 0; i < n; i++) {
        int64_t hri = (int64_t)hr[i], hii = (int64_t)hi[i];
        int64_t yr = p210_op_rhe(xr[i] * hri - xi[i] * hii, P210_OP_TABLE_FRAC);
        int64_t yi = p210_op_rhe(xr[i] * hii + xi[i] * hri, P210_OP_TABLE_FRAC);
        xr[i] = p210_op_clamp24(yr);
        xi[i] = p210_op_clamp24(yi);
    }
}

void p210_op_modrelu(int64_t *zr, int64_t *zi, unsigned n,
                     int64_t b_q23, int block_exp)
{
    int64_t b_eff;
    if (block_exp > 0) {
        b_eff = p210_op_rhe(b_q23, (unsigned)block_exp);
    } else if (block_exp < 0) {
        b_eff = b_q23 << (unsigned)(-block_exp);
    } else {
        b_eff = b_q23;
    }
    for (unsigned i = 0; i < n; i++) {
        int64_t ar = zr[i] < 0 ? -zr[i] : zr[i];
        int64_t ai = zi[i] < 0 ? -zi[i] : zi[i];
        int64_t mx = ar > ai ? ar : ai;
        int64_t mn = ar > ai ? ai : ar;
        int64_t mag = p210_op_rhe(15 * mx, 4) + p210_op_rhe(15 * mn, 5);
        int64_t keep = mag + b_eff;
        int64_t scale = 0;
        if (keep > 0 && mag > 0) {
            scale = (keep << P210_OP_TABLE_FRAC) / mag; /* floor, nonneg */
        }
        zr[i] = p210_op_clamp24(p210_op_rhe(zr[i] * scale, P210_OP_TABLE_FRAC));
        zi[i] = p210_op_clamp24(p210_op_rhe(zi[i] * scale, P210_OP_TABLE_FRAC));
    }
}

uint64_t p210_op_splitmix64_next(uint64_t *state)
{
    uint64_t z = (*state += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

void p210_op_vector_input(uint64_t seed, unsigned n, int64_t *re, int64_t *im)
{
    uint64_t state = seed;
    for (unsigned i = 0; i < n; i++) {
        uint64_t w = p210_op_splitmix64_next(&state);
        int64_t r16 = (int64_t)(w & 0xFFFFu);
        int64_t i16 = (int64_t)((w >> 16) & 0xFFFFu);
        if (r16 >= 32768) {
            r16 -= 65536;
        }
        if (i16 >= 32768) {
            i16 -= 65536;
        }
        re[i] = r16 << 8;
        im[i] = i16 << 8;
    }
}
