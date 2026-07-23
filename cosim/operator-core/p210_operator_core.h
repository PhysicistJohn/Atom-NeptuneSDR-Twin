/*
 * P210 spectral neural-operator: golden-arithmetic v1 compute core.
 *
 * This is the bit-exactness substance of the v2 operator device: a pure-C,
 * dependency-free implementation of the golden integer arithmetic defined by
 * Atom-Neural-RL (src/atom_neural_rl/golden.py) and pinned by its committed
 * test-vector digests. The QEMU MMIO device wraps this core; the RTL implements
 * the same operations. Nothing here depends on QEMU: it compiles standalone so
 * the C==Python equivalence is provable with cc alone.
 *
 * Golden-arithmetic v1, in brief:
 *  - data: 24-bit signed complex components carried in int64
 *  - twiddles: Q1.17 from the committed ROM (32768 cos/sin pairs over [0, pi));
 *    FFT twiddle W_N^k = (cos, -sin) at ROM index k * (65536 / N)
 *  - ONE rounding rule everywhere: round-half-to-even right shift (rhe)
 *  - FFT: radix-2 DIT, bit-reversed input, natural output, rhe(.,1) per stage
 *    (forward computes FFT/N); IFFT = conj -> forward -> conj
 *  - spectral multiply: Q1.15 table mantissas, rhe(.,15)
 *  - modReLU: alpha-max-beta-min magnitude (15/16, 15/32), exponent-compensated
 *    threshold, survivor scale ((m+b)<<15)/m by floor division
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef P210_OPERATOR_CORE_H
#define P210_OPERATOR_CORE_H

#include <stdint.h>
#include <stddef.h>

#define P210_OP_DATA_BITS      24
#define P210_OP_DATA_MAX       ((int64_t)((1 << (P210_OP_DATA_BITS - 1)) - 1))
#define P210_OP_DATA_MIN       (-(int64_t)(1 << (P210_OP_DATA_BITS - 1)))
#define P210_OP_TWIDDLE_FRAC   17
#define P210_OP_TABLE_FRAC     15
#define P210_OP_ROM_HALF_TURN  32768
#define P210_OP_ROM_RESOLUTION 65536

/* Round-half-to-even arithmetic right shift -- THE rounding rule. */
int64_t p210_op_rhe(int64_t v, unsigned s);

/* Clamp to the 24-bit data range. */
int64_t p210_op_clamp24(int64_t v);

/* Provide the twiddle ROM (interleaved int32 cos,sin; 65536 words). The caller
 * owns the memory; the core keeps the pointer. Returns 0 on success. */
int p210_op_set_rom(const int32_t *rom, size_t words);

/* In-place forward golden FFT of n samples (n a power of two <= 65536).
 * re/im are natural-order 24-bit samples; outputs natural order, = FFT/N. */
int p210_op_fft(int64_t *re, int64_t *im, unsigned n);

/* Inverse: conj -> forward -> conj (exactly the true IFFT). */
int p210_op_ifft(int64_t *re, int64_t *im, unsigned n);

/* Per-bin spectral multiply by Q1.15 mantissa tables. */
void p210_op_spectral_multiply(int64_t *xr, int64_t *xi,
                               const int16_t *hr, const int16_t *hi, unsigned n);

/* Integer modReLU with exponent-compensated threshold (b_q23 <= 0, Q1.23 at
 * block exponent 0). */
void p210_op_modrelu(int64_t *zr, int64_t *zi, unsigned n,
                     int64_t b_q23, int block_exp);

/* The pinned test-vector PRNG (splitmix64) and input mapping. */
uint64_t p210_op_splitmix64_next(uint64_t *state);
void p210_op_vector_input(uint64_t seed, unsigned n, int64_t *re, int64_t *im);

#endif /* P210_OPERATOR_CORE_H */
