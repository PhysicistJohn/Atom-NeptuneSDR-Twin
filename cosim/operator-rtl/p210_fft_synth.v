// P210 golden-arithmetic v1 FFT -- SYNTHESIZABLE engine.
//
// Unlike p210_fft_engine.v (written for simulation clarity: in-place array with
// an async read port that will not map to BRAM), this engine synthesizes on the
// xc7z020: a true dual-port BRAM sample memory with synchronous reads and a
// DSP-mappable butterfly. It is bit-exact to golden-arithmetic v1 (18-bit ROM
// twiddles, round-half-to-even schedule, radix-2 DIT stage order) and verified
// against the golden vectors in tb_fft_synth.v.
//
// Datapath: one true dual-port BRAM per component (re, im). Each butterfly
// reads both operands (ports A/B) in one cycle, latches, computes, and writes
// both results back in-place -- read and write are in separate FSM states, so
// in-place is hazard-free. A pipelined one-butterfly-per-cycle version is a
// throughput optimization on top of this; correctness and BRAM/DSP mapping are
// what this proves.
//
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps

// ---- true dual-port BRAM, synchronous read (write-first not required) -------
module tdp_bram #(parameter WIDTH = 24, parameter AW = 10) (
    input  wire                    clk,
    input  wire                    we_a,
    input  wire [AW-1:0]           addr_a,
    input  wire signed [WIDTH-1:0] din_a,
    output reg  signed [WIDTH-1:0] dout_a,
    input  wire                    we_b,
    input  wire [AW-1:0]           addr_b,
    input  wire signed [WIDTH-1:0] din_b,
    output reg  signed [WIDTH-1:0] dout_b
);
    (* ram_style = "block" *) reg signed [WIDTH-1:0] mem [0:(1<<AW)-1];
    always @(posedge clk) begin
        if (we_a) mem[addr_a] <= din_a;
        dout_a <= mem[addr_a];
    end
    always @(posedge clk) begin
        if (we_b) mem[addr_b] <= din_b;
        dout_b <= mem[addr_b];
    end
endmodule

module p210_fft_synth #(
    parameter LOG2N = 10,
    parameter ROM_FILE_COS = "rom_cos.memh",
    parameter ROM_FILE_SIN = "rom_sin.memh"
) (
    input  wire                clk,
    input  wire                rst,
    input  wire                start,
    output reg                 done,
    input  wire                ld_we,       // host load (bit-reversed internally)
    input  wire [LOG2N-1:0]    io_addr,     // natural-order host index
    input  wire signed [23:0]  ld_re, ld_im,
    output wire signed [23:0]  rd_re, rd_im
);
    localparam N = 1 << LOG2N;
    localparam [LOG2N:0] HALFN = N >> 1;

    function signed [47:0] rhe;
        input signed [47:0] v; input integer s;
        reg signed [47:0] q, half, mask;
        begin
            if (s == 0) rhe = v;
            else begin
                half = 48'sd1 <<< (s - 1);
                mask = (48'sd1 <<< s) - 1;
                q = (v + half) >>> s;
                if (((v & mask) == half) && q[0]) q = q - 1;
                rhe = q;
            end
        end
    endfunction
    function signed [23:0] clamp24;
        input signed [47:0] v;
        begin
            if (v > 48'sd8388607) clamp24 = 24'sd8388607;
            else if (v < -48'sd8388608) clamp24 = -24'sd8388608;
            else clamp24 = v[23:0];
        end
    endfunction
    function [LOG2N-1:0] bitrev;
        input [LOG2N-1:0] v; integer i;
        begin bitrev = 0; for (i=0;i<LOG2N;i=i+1) bitrev[LOG2N-1-i]=v[i]; end
    endfunction

    (* rom_style = "block" *) reg signed [17:0] rom_cos [0:32767];
    (* rom_style = "block" *) reg signed [17:0] rom_sin [0:32767];
    initial begin $readmemh(ROM_FILE_COS, rom_cos); $readmemh(ROM_FILE_SIN, rom_sin); end

    // one TDP BRAM for re, one for im
    reg              we_a, we_b;
    reg  [LOG2N-1:0] addr_a, addr_b;
    reg  signed [23:0] din_a_re, din_b_re, din_a_im, din_b_im;
    wire signed [23:0] q_a_re, q_b_re, q_a_im, q_b_im;
    tdp_bram #(24, LOG2N) mre (.clk(clk), .we_a(we_a), .addr_a(addr_a), .din_a(din_a_re), .dout_a(q_a_re),
                                         .we_b(we_b), .addr_b(addr_b), .din_b(din_b_re), .dout_b(q_b_re));
    tdp_bram #(24, LOG2N) mim (.clk(clk), .we_a(we_a), .addr_a(addr_a), .din_a(din_a_im), .dout_a(q_a_im),
                                         .we_b(we_b), .addr_b(addr_b), .din_b(din_b_im), .dout_b(q_b_im));
    assign rd_re = q_a_re;
    assign rd_im = q_a_im;

    localparam S_IDLE=0, S_RD=1, S_LAT=2, S_P1=3, S_P2=4, S_WR=5, S_DONE=6;
    reg [2:0]        state;
    reg [4:0]        stage;
    reg signed [17:0] tcos, tsin;
    reg [LOG2N-1:0]  ia_r, ib_r;

    // pipelined butterfly registers (break the long combinational chain that
    // real synthesis showed does not close timing unpipelined):
    //   S_P1: register the four DSP products + pass a  (BRAM-out -> multiply)
    //   S_P2: register tr,ti = rhe(sum,17) + pass a     (rounding stage)
    //   S_WR: y = clamp(rhe(a +/- tr, 1)); write        (add + round + clamp)
    reg signed [47:0] pr_brc, pr_bis, pr_bic, pr_brs;   // products
    reg signed [23:0] ar_p, ai_p, ar_pp, ai_pp;
    reg signed [47:0] tr_r, ti_r;

    // Incremental address generation: no per-butterfly variable shifts (which
    // synthesize to barrel shifters). Per-stage constants half/step/tw_stride
    // are maintained by doubling/halving once per stage; ia/ib are adds and
    // tw_idx is an accumulator. Bit-identical butterfly order to the shift-based
    // decode: ia = group_base + p, ib = ia + half, tw = p * tw_stride.
    reg [LOG2N:0]    half, step;      // 2^stage, 2^(stage+1)
    reg [15:0]       tw_stride;       // 2^(15-stage)
    reg [LOG2N:0]    group_base;
    reg [LOG2N-1:0]  p;
    reg [14:0]       tw_idx;

    wire [LOG2N-1:0] ia   = group_base[LOG2N-1:0] + p;
    wire [LOG2N-1:0] ib   = group_base[LOG2N-1:0] + p + half[LOG2N-1:0];
    wire [14:0]      tw_i = tw_idx;

    // butterfly computed directly from the (now valid) BRAM outputs in S_WR
    always @(posedge clk) begin
        we_a<=0; we_b<=0;
        if (rst) begin state<=S_IDLE; done<=0; end
        else begin
            case (state)
            S_IDLE: begin
                done<=0;
                if (ld_we) begin we_a<=1; addr_a<=bitrev(io_addr); din_a_re<=ld_re; din_a_im<=ld_im; end
                else addr_a<=io_addr;                 // host read port
                if (start) begin
                    stage<=0; half<=1; step<=2; tw_stride<=15'sd0 + (1<<15);
                    group_base<=0; p<=0; tw_idx<=0;
                    state<=S_RD;
                end
            end
            S_RD: begin                                // issue read of ia, ib (2-cycle sync path)
                addr_a<=ia; addr_b<=ib;
                tcos<=rom_cos[tw_i]; tsin<=rom_sin[tw_i];
                ia_r<=ia; ib_r<=ib;
                state<=S_LAT;
            end
            S_LAT: state<=S_P1;                        // wait: BRAM data valid next cycle
            S_P1: begin                                // q valid: register the 4 products + a
                pr_brc <= $signed(q_b_re)*$signed(tcos);
                pr_bis <= $signed(q_b_im)*$signed(tsin);
                pr_bic <= $signed(q_b_im)*$signed(tcos);
                pr_brs <= $signed(q_b_re)*$signed(tsin);
                ar_p <= q_a_re; ai_p <= q_a_im;
                state<=S_P2;
            end
            S_P2: begin                                // rounding stage: tr,ti = rhe(sum,17)
                tr_r <= rhe(pr_brc + pr_bis, 17);
                ti_r <= rhe(pr_bic - pr_brs, 17);
                ar_pp <= ar_p; ai_pp <= ai_p;
                state<=S_WR;
            end
            S_WR: begin                                // add +/- , round /2, clamp; write back
                we_a<=1; addr_a<=ia_r; din_a_re<=clamp24(rhe($signed(ar_pp)+tr_r,1)); din_a_im<=clamp24(rhe($signed(ai_pp)+ti_r,1));
                we_b<=1; addr_b<=ib_r; din_b_re<=clamp24(rhe($signed(ar_pp)-tr_r,1)); din_b_im<=clamp24(rhe($signed(ai_pp)-ti_r,1));
                if (p + 1 == half[LOG2N-1:0]) begin         // last butterfly in this group
                    p<=0; tw_idx<=0;
                    if (group_base + step >= N) begin       // last group in this stage
                        if (stage == LOG2N-1) state<=S_DONE;
                        else begin
                            stage<=stage+1; half<=half<<1; step<=step<<1;
                            tw_stride<=tw_stride>>1; group_base<=0; state<=S_RD;
                        end
                    end else begin
                        group_base<=group_base+step; state<=S_RD;
                    end
                end else begin
                    p<=p+1; tw_idx<=tw_idx+tw_stride; state<=S_RD;
                end
            end
            S_DONE: begin done<=1; addr_a<=io_addr; if (start) state<=S_DONE; else state<=S_IDLE; end
            default: state<=S_IDLE;
            endcase
        end
    end
endmodule
