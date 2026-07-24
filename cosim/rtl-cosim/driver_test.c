/*
 * Standalone driver: run the Verilated RTL block through the rtl_block ABI and
 * check it matches the golden vectors. This is the same driver logic the twin's
 * RTL co-processor device uses, exercised without QEMU so the RTL+shim is proven
 * on its own. Reads the committed golden .memh (input + expected).
 *
 * Usage: driver_test <in_re.memh> <in_im.memh> <exp_re.memh> <exp_im.memh>
 * SPDX-License-Identifier: MIT
 */
#include "rtl_block.h"
#include <stdio.h>
#include <stdlib.h>

static int load_memh(const char *path, int32_t *out, int n)
{
    FILE *f = fopen(path, "r");
    if (!f) { perror(path); return -1; }
    char line[64];
    int i = 0;
    while (i < n && fgets(line, sizeof line, f)) {
        unsigned v = (unsigned)strtoul(line, NULL, 16);
        out[i++] = (int32_t)(v << 8) >> 8;   /* 24-bit sign-extend */
    }
    fclose(f);
    return i == n ? 0 : -1;
}

int main(int argc, char **argv)
{
    if (argc != 5) { fprintf(stderr, "usage: %s in_re in_im exp_re exp_im\n", argv[0]); return 2; }
    const uint32_t N = 1u << 10;
    static int32_t in_re[1024], in_im[1024], exp_re[1024], exp_im[1024];
    if (load_memh(argv[1], in_re, N) || load_memh(argv[2], in_im, N) ||
        load_memh(argv[3], exp_re, N) || load_memh(argv[4], exp_im, N)) {
        fprintf(stderr, "failed to load vectors\n"); return 2;
    }

    rtl_block *b = rtl_open();
    for (uint32_t i = 0; i < N; i++) rtl_load(b, i, in_re[i], in_im[i]);
    rtl_start(b, 10);
    if (rtl_run(b, 200000) != 0) { fprintf(stderr, "RTL did not finish\n"); rtl_close(b); return 1; }

    int errors = 0;
    for (uint32_t i = 0; i < N; i++) {
        int32_t re, im;
        rtl_read(b, i, &re, &im);
        if (re != exp_re[i] || im != exp_im[i]) {
            if (errors < 5)
                printf("  bin %u: got (%d,%d) want (%d,%d)\n", i, re, im, exp_re[i], exp_im[i]);
            errors++;
        }
    }
    rtl_close(b);
    if (errors == 0) {
        printf("P210_RTL_COSIM PASS (Verilated RTL == golden through the rtl_block ABI, N=%u)\n", N);
        return 0;
    }
    printf("P210_RTL_COSIM FAIL (%d bins)\n", errors);
    return 1;
}
