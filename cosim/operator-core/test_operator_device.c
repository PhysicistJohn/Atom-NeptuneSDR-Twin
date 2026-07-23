/*
 * Standalone equivalence test for the v2 operator device: drives the full ABI
 * transaction (weight-bank load -> CRC gate -> ACTIVATE -> configure -> START)
 * against a flat DDR model and verifies the block output reproduces the golden
 * full-operator vector (FFT -> spectral multiply -> IFFT -> modReLU), the same
 * pinned digest the Python reference and RTL produce. Also checks the CRC gate
 * rejects a corrupted bank (BANK_INTEGRITY) and that result-weight attribution
 * is latched.
 *
 * Build: cc -O2 test_operator_device.c p210_operator_device.c p210_operator_core.c -lz -o test_operator_device
 * Run:   ./test_operator_device <twiddle-rom-q117.bin>
 * SPDX-License-Identifier: MIT
 */
#include "p210_operator_device.h"
#include "p210_operator_core.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

/* --- minimal SHA-256 (same as test_operator_core.c) --- */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t off; } sha_t;
static const uint32_t KK[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
#define RR(x,n) (((x)>>(n))|((x)<<(32-(n))))
static void shi(sha_t*s){static const uint32_t h0[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};memcpy(s->h,h0,32);s->len=0;s->off=0;}
static void shb(sha_t*s,const uint8_t*p){uint32_t w[64],a,b,c,d,e,f,g,h;for(int i=0;i<16;i++)w[i]=(uint32_t)p[4*i]<<24|(uint32_t)p[4*i+1]<<16|(uint32_t)p[4*i+2]<<8|p[4*i+3];for(int i=16;i<64;i++){uint32_t s0=RR(w[i-15],7)^RR(w[i-15],18)^(w[i-15]>>3),s1=RR(w[i-2],17)^RR(w[i-2],19)^(w[i-2]>>10);w[i]=w[i-16]+s0+w[i-7]+s1;}a=s->h[0];b=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];for(int i=0;i<64;i++){uint32_t S1=RR(e,6)^RR(e,11)^RR(e,25),ch=(e&f)^((~e)&g),t1=h+S1+ch+KK[i]+w[i],S0=RR(a,2)^RR(a,13)^RR(a,22),mj=(a&b)^(a&c)^(b&c),t2=S0+mj;h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;}
static void shu(sha_t*s,const void*data,size_t n){const uint8_t*p=data;s->len+=n;while(n){size_t t=64-s->off;if(t>n)t=n;memcpy(s->buf+s->off,p,t);s->off+=t;p+=t;n-=t;if(s->off==64){shb(s,s->buf);s->off=0;}}}
static void shf(sha_t*s,char o[65]){uint64_t bits=s->len*8;uint8_t pad=0x80,z=0;shu(s,&pad,1);while(s->off!=56)shu(s,&z,1);uint8_t lb[8];for(int i=0;i<8;i++)lb[i]=(uint8_t)(bits>>(56-8*i));shu(s,lb,8);for(int i=0;i<8;i++)sprintf(o+8*i,"%08x",s->h[i]);o[64]=0;}

static void put32(uint8_t *p, int32_t v){ p[0]=v; p[1]=v>>8; p[2]=v>>16; p[3]=v>>24; }

int main(int argc, char **argv)
{
    if (argc != 2) { fprintf(stderr, "usage: %s <twiddle-rom-q117.bin>\n", argv[0]); return 2; }
    FILE *fh = fopen(argv[1], "rb"); if (!fh) { perror("rom"); return 2; }
    static int32_t rom[65536];
    if (fread(rom, 4, 65536, fh) != 65536) { fprintf(stderr, "bad rom\n"); return 2; }
    fclose(fh);
    p210_op_set_rom(rom, 65536);

    const uint32_t N = 256, log2n = 8;
    const int32_t B_Q23 = -300000;

    /* --- DDR model with the ABI memory layout --- */
    P210OperatorDevice dev; memset(&dev, 0, sizeof dev);
    dev.ddr_base = 0x18000000; dev.ddr_size = 0x01000000;   /* 16 MB window */
    dev.ddr = calloc(1, dev.ddr_size);
    p210_dev_reset(&dev);

    uint32_t INPUT_ADDR = 0x18000000, OUTPUT2_ADDR = 0x18180000, WEIGHT_ADDR = 0x18200000;

    /* input: golden vector, written int32 re,im interleaved */
    static int64_t re[256], im[256];
    p210_op_vector_input(31, N, re, im);
    for (uint32_t i = 0; i < N; i++) {
        put32(dev.ddr + (INPUT_ADDR - dev.ddr_base) + 8*i,   (int32_t)re[i]);
        put32(dev.ddr + (INPUT_ADDR - dev.ddr_base) + 8*i+4, (int32_t)im[i]);
    }
    /* weight bank blob: [modes:u32][re:int16*N][im:int16*N], H = flat 0.5 (exp 0) */
    uint32_t blob_bytes = 4 + N*2 + N*2;
    uint8_t *blob = dev.ddr + (WEIGHT_ADDR - dev.ddr_base);
    memcpy(blob, &N, 4);
    for (uint32_t i = 0; i < N; i++) {
        int16_t hr = 1<<14, hi = 0;
        memcpy(blob + 4 + 2*i, &hr, 2);
        memcpy(blob + 4 + 2*N + 2*i, &hi, 2);
    }
    uint32_t bank_crc = (uint32_t)crc32(0, blob, blob_bytes);

    /* --- drive the ABI transaction --- */
    p210_dev_write32(&dev, P210_REG_LOG2_N, log2n);
    p210_dev_write32(&dev, P210_REG_INPUT_ADDR, INPUT_ADDR);
    p210_dev_write32(&dev, P210_REG_OUTPUT2_ADDR, OUTPUT2_ADDR);
    p210_dev_write32(&dev, P210_REG_OP_MODE_COUNT, N);
    p210_dev_write32(&dev, P210_REG_OP_OUTPUT_MODE, P210_OUT_COMPLEX);
    p210_dev_write32(&dev, P210_REG_OP_THRESHOLD, (uint32_t)B_Q23);
    p210_dev_write32(&dev, P210_REG_WEIGHT_ADDR, WEIGHT_ADDR);
    p210_dev_write32(&dev, P210_REG_WEIGHT_BYTES, blob_bytes);
    p210_dev_write32(&dev, P210_REG_WEIGHT_CRC, bank_crc);
    p210_dev_write32(&dev, P210_REG_OP_FLAGS, 0);            /* clear BYPASS -> operator path */
    p210_dev_write32(&dev, P210_REG_CONTROL, P210_CTRL_WEIGHT_ACTIVATE);

    int failures = 0;
    if (!(p210_dev_read32(&dev, P210_REG_STATUS) & P210_ST_WEIGHT_READY)) {
        printf("ACTIVATE: weight not ready\n"); failures++;
    }
    p210_dev_write32(&dev, P210_REG_CONTROL, P210_CTRL_START);
    if (!(p210_dev_read32(&dev, P210_REG_STATUS) & P210_ST_DONE)) { printf("block did not complete\n"); failures++; }

    /* gather output re[] then im[] and digest -- compare to the golden pin */
    sha_t s; shi(&s);
    uint8_t *o = dev.ddr + (OUTPUT2_ADDR - dev.ddr_base);
    for (uint32_t i = 0; i < N; i++) shu(&s, o + 8*i, 4);       /* re */
    for (uint32_t i = 0; i < N; i++) shu(&s, o + 8*i + 4, 4);   /* im */
    char digest[65]; shf(&s, digest);
    const char *PIN = "2b994fa7094492fb9bdd120708b512a835f039a424dc502fd61991fdb9c0901d";
    int match = strcmp(digest, PIN) == 0;
    printf("operator block via ABI: %s\n", match ? "MATCH golden" : "DIVERGED");
    if (!match) { printf("  got  %s\n  want %s\n", digest, PIN); failures++; }

    /* result-weight attribution latched */
    if (p210_dev_read32(&dev, P210_REG_RESULT_WT_CRC) != bank_crc) { printf("result-weight CRC not latched\n"); failures++; }
    if (p210_dev_read32(&dev, P210_REG_RESULT_SEQUENCE) != 1)      { printf("result sequence not advanced\n"); failures++; }

    /* CRC gate: corrupt the blob, re-activate, expect BANK_INTEGRITY */
    p210_dev_reset(&dev);
    blob[8] ^= 0xFF;
    p210_dev_write32(&dev, P210_REG_WEIGHT_ADDR, WEIGHT_ADDR);
    p210_dev_write32(&dev, P210_REG_WEIGHT_BYTES, blob_bytes);
    p210_dev_write32(&dev, P210_REG_WEIGHT_CRC, bank_crc);       /* stale CRC */
    p210_dev_write32(&dev, P210_REG_CONTROL, P210_CTRL_WEIGHT_ACTIVATE);
    if (!(p210_dev_read32(&dev, P210_REG_STATUS) & P210_ST_BANK_CRC_FAIL) ||
        p210_dev_read32(&dev, P210_REG_ERROR_CODE) != P210_ERR_BANK_INTEGRITY) {
        printf("CRC gate did not reject corrupted bank\n"); failures++;
    } else {
        printf("CRC gate: corrupted bank rejected (BANK_INTEGRITY)\n");
    }

    free(dev.ddr); free(dev.wt_re); free(dev.wt_im);
    if (failures == 0) { printf("P210_OPERATOR_DEVICE_ABI PASS (operator runs through the v2 contract, bit-exact)\n"); return 0; }
    return 1;
}
