/*
 * Standalone bit-exactness harness: proves the C core reproduces the golden
 * pinned vector digests from Atom-Neural-RL tests/test_golden.py, using the
 * committed twiddle ROM. Compiles with plain cc; no QEMU, no dependencies
 * beyond a small embedded SHA-256.
 *
 * Usage: test_operator_core <path-to-twiddle-rom-q117.bin>
 * Exit 0 iff every digest matches the pins.
 *
 * SPDX-License-Identifier: MIT
 */
#include "p210_operator_core.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------- minimal SHA-256 ------------------------------- */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t off; } sha256_t;
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
#define ROR(x,n) (((x)>>(n))|((x)<<(32-(n))))
static void sha_init(sha256_t *s){
    static const uint32_t h0[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                                 0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    memcpy(s->h,h0,sizeof h0); s->len=0; s->off=0;
}
static void sha_block(sha256_t *s,const uint8_t *p){
    uint32_t w[64],a,b,c,d,e,f,g,h;
    for(int i=0;i<16;i++) w[i]=(uint32_t)p[4*i]<<24|(uint32_t)p[4*i+1]<<16|(uint32_t)p[4*i+2]<<8|p[4*i+3];
    for(int i=16;i<64;i++){
        uint32_t s0=ROR(w[i-15],7)^ROR(w[i-15],18)^(w[i-15]>>3);
        uint32_t s1=ROR(w[i-2],17)^ROR(w[i-2],19)^(w[i-2]>>10);
        w[i]=w[i-16]+s0+w[i-7]+s1;
    }
    a=s->h[0];b=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
    for(int i=0;i<64;i++){
        uint32_t S1=ROR(e,6)^ROR(e,11)^ROR(e,25);
        uint32_t ch=(e&f)^((~e)&g);
        uint32_t t1=h+S1+ch+K[i]+w[i];
        uint32_t S0=ROR(a,2)^ROR(a,13)^ROR(a,22);
        uint32_t mj=(a&b)^(a&c)^(b&c);
        uint32_t t2=S0+mj;
        h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static void sha_update(sha256_t *s,const void *data,size_t n){
    const uint8_t *p=data; s->len+=n;
    while(n){
        size_t take=64-s->off; if(take>n) take=n;
        memcpy(s->buf+s->off,p,take); s->off+=take; p+=take; n-=take;
        if(s->off==64){ sha_block(s,s->buf); s->off=0; }
    }
}
static void sha_final(sha256_t *s,char out[65]){
    uint64_t bits=s->len*8; uint8_t pad=0x80;
    sha_update(s,&pad,1); uint8_t z=0;
    while(s->off!=56) sha_update(s,&z,1);
    uint8_t lenb[8]; for(int i=0;i<8;i++) lenb[i]=(uint8_t)(bits>>(56-8*i));
    sha_update(s,lenb,8);
    for(int i=0;i<8;i++) sprintf(out+8*i,"%08x",s->h[i]);
    out[64]=0;
}
/* ------------------------------------------------------------------------- */

static void digest_arrays(const int64_t *re, const int64_t *im, unsigned n, char out[65])
{
    /* int32-LE serialization of re then im, matching golden.digest() */
    sha256_t s; sha_init(&s);
    for (unsigned i = 0; i < n; i++) {
        int32_t v = (int32_t)re[i];
        uint8_t b[4] = {(uint8_t)v,(uint8_t)(v>>8),(uint8_t)(v>>16),(uint8_t)(v>>24)};
        sha_update(&s,b,4);
    }
    for (unsigned i = 0; i < n; i++) {
        int32_t v = (int32_t)im[i];
        uint8_t b[4] = {(uint8_t)v,(uint8_t)(v>>8),(uint8_t)(v>>16),(uint8_t)(v>>24)};
        sha_update(&s,b,4);
    }
    sha_final(&s,out);
}

struct pin { const char *kind; uint64_t seed; unsigned n; const char *sha; };
static const struct pin PINS[] = {
    {"fft", 11, 256,  "ccea7a8301f8b8372bb1d2365b4e06e7330200ffee249e0f1634210fb9dd7a22"},
    {"fft", 12, 1024, "09ea67d3e313581054387508d4709b2dc6b04cdc26d2a5036dd024a4037b82b1"},
    {"fft", 13, 4096, "481b10ecbe9823d42e3e279ca88f76aba2e7e2e7229926cb3bd81edfc8f8aa80"},
    {"roundtrip", 21, 1024, "cd276c1485d97ce30b707aa9a9cd08d4c6f8170333020e0413f740a29bfcc0d2"},
};

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <twiddle-rom-q117.bin>\n", argv[0]);
        return 2;
    }
    FILE *fh = fopen(argv[1], "rb");
    if (!fh) { perror("rom"); return 2; }
    static int32_t rom[P210_OP_ROM_HALF_TURN * 2];
    size_t got = fread(rom, sizeof(int32_t), P210_OP_ROM_HALF_TURN * 2, fh);
    fclose(fh);
    if (got != (size_t)P210_OP_ROM_HALF_TURN * 2 || p210_op_set_rom(rom, got) != 0) {
        fprintf(stderr, "bad ROM\n");
        return 2;
    }

    int failures = 0;
    for (size_t c = 0; c < sizeof PINS / sizeof PINS[0]; c++) {
        const struct pin *p = &PINS[c];
        static int64_t re[65536], im[65536];
        p210_op_vector_input(p->seed, p->n, re, im);
        if (strcmp(p->kind, "fft") == 0) {
            p210_op_fft(re, im, p->n);
        } else {
            p210_op_fft(re, im, p->n);
            static int16_t hr[65536], hi[65536];
            for (unsigned i = 0; i < p->n; i++) { hr[i] = 1 << 14; hi[i] = 0; }
            p210_op_spectral_multiply(re, im, hr, hi, p->n);
            p210_op_ifft(re, im, p->n);
        }
        char sha[65];
        digest_arrays(re, im, p->n, sha);
        int ok = strcmp(sha, p->sha) == 0;
        printf("%-9s seed=%llu N=%-5u %s\n", p->kind,
               (unsigned long long)p->seed, p->n, ok ? "MATCH" : "DIVERGED");
        if (!ok) {
            printf("  got  %s\n  want %s\n", sha, p->sha);
            failures++;
        }
    }
    if (failures == 0) {
        printf("P210_OPERATOR_CORE_BITEXACT PASS (C == Python golden)\n");
        return 0;
    }
    return 1;
}
