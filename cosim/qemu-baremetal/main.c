/*
 * Bare-metal operator drive: runs on the emulated Cortex-A9 and drives the P210
 * v2 operator device at 0x7c450000 through MMIO + DMA, exactly as guest firmware
 * would. Generates the golden input on-device, loads a weight bank, activates,
 * starts, and compares the block output to the golden expected vector. This
 * closes the "driven from the emulated CPU, not external qtest" caveat.
 *
 * Output via ARM semihosting; run under QEMU with -semihosting.
 * SPDX-License-Identifier: MIT
 */
#include <stdint.h>
#include "expected_op256.h"

#define OP_BASE      0x7C450000u
#define INPUT_ADDR   0x18000000u
#define OUTPUT2_ADDR 0x18180000u
#define WEIGHT_ADDR  0x18200000u
#define N            256u
#define LOG2N        8u

/* register offsets */
#define R_CONTROL   0x00c
#define R_STATUS    0x010
#define R_LOG2_N    0x018
#define R_INPUT     0x024
#define R_RESULT_SEQ 0x038
#define R_MODE_CNT  0x084
#define R_OUT_MODE  0x088
#define R_FLAGS     0x08c
#define R_WADDR     0x090
#define R_WBYTES    0x094
#define R_WCRC      0x0a0
#define R_RESULT_WT_CRC 0x0ac
#define R_OUTPUT2   0x0dc
#define R_THRESH    0x0e4
#define CTRL_ACTIVATE 0x400u
#define CTRL_START    0x001u
#define ST_DONE       0x02u
#define ST_WT_READY   0x10u

static volatile uint32_t *REG(uint32_t off) { return (volatile uint32_t *)(uintptr_t)(OP_BASE + off); }
static volatile int32_t  *MEM32(uint32_t a) { return (volatile int32_t *)(uintptr_t)a; }
static volatile int16_t  *MEM16(uint32_t a) { return (volatile int16_t *)(uintptr_t)a; }

/* --- ARM semihosting --- */
static void sh_write0(const char *s)
{
    register int r0 asm("r0") = 0x04;
    register const char *r1 asm("r1") = s;
    asm volatile("svc 0x123456" : : "r"(r0), "r"(r1) : "memory");
}
static void sh_exit(void)
{
    register int r0 asm("r0") = 0x18;
    register int r1 asm("r1") = 0x20026;
    asm volatile("svc 0x123456" : : "r"(r0), "r"(r1));
    for (;;) {}
}

/* --- golden input PRNG (splitmix64) + IEEE CRC32, matching the reference --- */
static uint64_t sm_state;
static uint64_t sm_next(void)
{
    uint64_t z = (sm_state += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}
static uint32_t crc32(volatile const uint8_t *p, uint32_t n)
{
    uint32_t c = 0xFFFFFFFFu, i; int k;
    for (i = 0; i < n; i++) {
        c ^= p[i];
        for (k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1)));
        }
    }
    return c ^ 0xFFFFFFFFu;
}

int main(void)
{
    uint32_t i, wbytes;
    volatile int32_t *in = MEM32(INPUT_ADDR);
    volatile int32_t *out = MEM32(OUTPUT2_ADDR);
    volatile uint8_t *blob = (volatile uint8_t *)(uintptr_t)WEIGHT_ADDR;

    /* 1. golden input into DRAM */
    sm_state = 31;
    for (i = 0; i < N; i++) {
        uint64_t w = sm_next();
        int32_t r = (int32_t)(w & 0xFFFF), q = (int32_t)((w >> 16) & 0xFFFF);
        if (r >= 32768) r -= 65536;
        if (q >= 32768) q -= 65536;
        in[2 * i] = r << 8; in[2 * i + 1] = q << 8;
    }
    /* 2. weight bank blob: [modes u32][hr i16*N][hi i16*N], H = flat 0.5 */
    *(volatile uint32_t *)blob = N;
    for (i = 0; i < N; i++) {
        MEM16(WEIGHT_ADDR + 4)[i] = (int16_t)(1 << 14);
        MEM16(WEIGHT_ADDR + 4 + 2 * N)[i] = 0;
    }
    wbytes = 4 + 2 * N + 2 * N;

    /* 3. configure + drive the ABI transaction over MMIO */
    *REG(R_LOG2_N) = LOG2N;
    *REG(R_INPUT) = INPUT_ADDR;
    *REG(R_OUTPUT2) = OUTPUT2_ADDR;
    *REG(R_MODE_CNT) = N;
    *REG(R_OUT_MODE) = 1;
    *REG(R_THRESH) = (uint32_t)(-300000);
    *REG(R_WADDR) = WEIGHT_ADDR;
    *REG(R_WBYTES) = wbytes;
    *REG(R_WCRC) = crc32(blob, wbytes);
    *REG(R_FLAGS) = 0;
    *REG(R_CONTROL) = CTRL_ACTIVATE;
    if (!(*REG(R_STATUS) & ST_WT_READY)) { sh_write0("FAIL activate\n"); sh_exit(); }
    *REG(R_CONTROL) = CTRL_START;
    if (!(*REG(R_STATUS) & ST_DONE)) { sh_write0("FAIL not done\n"); sh_exit(); }

    /* 4. compare device output to the golden expected vector */
    for (i = 0; i < N; i++) {
        if (out[2 * i] != expected_re[i] || out[2 * i + 1] != expected_im[i]) {
            sh_write0("FAIL output mismatch\n"); sh_exit();
        }
    }
    if (*REG(R_RESULT_SEQ) != 1) { sh_write0("FAIL result seq\n"); sh_exit(); }

    sh_write0("P210_OPERATOR_BAREMETAL PASS (operator driven from the emulated CPU, bit-exact)\n");
    sh_exit();
    return 0;
}
