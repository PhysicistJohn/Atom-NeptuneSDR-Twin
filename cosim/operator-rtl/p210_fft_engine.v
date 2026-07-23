// P210 golden-arithmetic v1 FFT engine -- synthesizable-structure RTL.
//
// Implements the golden integer radix-2 DIT FFT exactly: 24-bit data, Q1.17
// ROM twiddles (the committed artifact, loaded via $readmemh; BRAM init in
// synthesis), round-half-to-even at the twiddle product (>>17) and at the
// per-stage divide (>>1). One butterfly per two clocks (read, then
// compute+write): sequential FSM over stages/groups, BRAM-style memories.
// Forward computes FFT/N in natural order from natural-order input
// (bit-reversal permutation on entry).
//
// Twiddle addressing: for stage s, position j, the golden twiddle is
// W_N^{j*(N/2^{s+1})}, whose ROM index is j << (15 - s) -- always < 32768.
// The FFT twiddle is (cos, -sin), expanded sign-explicitly below.
//
// This module proves the RTL leg of the bit-exactness chain in simulation
// against the pinned golden vectors. Resource/timing closure on xc7z020 needs
// Vivado and remains an open gap; the structure (one 24x18 multiplier pair per
// butterfly, BRAM memories, ROM twiddles) maps directly onto DSP48E1 + BRAM.
//
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps

module p210_fft_engine #(
    parameter LOG2N_MAX = 12,
    parameter ROM_FILE_COS = "rom_cos.memh",
    parameter ROM_FILE_SIN = "rom_sin.memh"
) (
    input  wire                   clk,
    input  wire                   rst,
    input  wire                   start,
    input  wire [4:0]             log2n,
    output reg                    done,
    input  wire                   mem_we,
    input  wire [LOG2N_MAX-1:0]   mem_addr,
    input  wire signed [23:0]     mem_wr_re,
    input  wire signed [23:0]     mem_wr_im,
    output wire signed [23:0]     mem_rd_re,
    output wire signed [23:0]     mem_rd_im
);
    localparam NMAX = 1 << LOG2N_MAX;

    reg signed [23:0] ram_re [0:NMAX-1];
    reg signed [23:0] ram_im [0:NMAX-1];
    reg signed [17:0] rom_cos [0:32767];
    reg signed [17:0] rom_sin [0:32767];

    initial begin
        $readmemh(ROM_FILE_COS, rom_cos);
        $readmemh(ROM_FILE_SIN, rom_sin);
    end

    assign mem_rd_re = ram_re[mem_addr];
    assign mem_rd_im = ram_im[mem_addr];

    // ---- round-half-to-even right shift (THE rounding rule) ----
    function signed [63:0] rhe;
        input signed [63:0] v;
        input integer s;
        reg signed [63:0] q;
        reg signed [63:0] mask, half;
        begin
            if (s == 0) rhe = v;
            else begin
                half = 64'sd1 <<< (s - 1);
                mask = (64'sd1 <<< s) - 1;
                q = (v + half) >>> s;
                if (((v & mask) == half) && q[0])
                    q = q - 1;
                rhe = q;
            end
        end
    endfunction

    function signed [23:0] clamp24;
        input signed [63:0] v;
        begin
            if (v > 64'sd8388607) clamp24 = 24'sd8388607;
            else if (v < -64'sd8388608) clamp24 = -24'sd8388608;
            else clamp24 = v[23:0];
        end
    endfunction

    localparam S_IDLE = 3'd0, S_PERM = 3'd1, S_BFLY_RD = 3'd2,
               S_BFLY_WR = 3'd3, S_DONE = 3'd4;
    reg [2:0]  state;
    reg [4:0]  nbits;
    reg [4:0]  stage;
    reg [LOG2N_MAX:0] perm_i;
    reg [LOG2N_MAX:0] group, pos;

    reg signed [23:0] a_re, a_im, b_re, b_im;
    reg signed [17:0] tw_cos, tw_sin;
    reg [LOG2N_MAX-1:0] ia_q, ib_q;

    function [LOG2N_MAX-1:0] bitrev;
        input [LOG2N_MAX-1:0] v;
        input [4:0] bits;
        integer bi;
        begin
            bitrev = 0;
            for (bi = 0; bi < LOG2N_MAX; bi = bi + 1)
                if (bi < bits)
                    bitrev[bits - 1 - bi] = v[bi];
        end
    endfunction

    wire [LOG2N_MAX:0] n_full = (1 << log2n);
    wire [LOG2N_MAX:0] half_w = (1 << stage);
    wire [LOG2N_MAX:0] step_w = (1 << (stage + 1));
    wire [14:0] rom_idx = pos[14:0] << (15 - stage);
    wire [LOG2N_MAX-1:0] perm_lo = perm_i[LOG2N_MAX-1:0];
    wire [LOG2N_MAX-1:0] perm_rev = bitrev(perm_lo, nbits);

    reg signed [63:0] tr64, ti64;

    always @(posedge clk) begin
        if (rst) begin
            state <= S_IDLE; done <= 1'b0;
        end else begin
            if (mem_we) begin
                ram_re[mem_addr] <= mem_wr_re;
                ram_im[mem_addr] <= mem_wr_im;
            end
            case (state)
            S_IDLE: begin
                done <= 1'b0;
                if (start) begin
                    nbits <= log2n;
                    perm_i <= 0;
                    state <= S_PERM;
                end
            end
            S_PERM: begin
                if (perm_i < n_full) begin
                    if (perm_rev > perm_lo) begin
                        ram_re[perm_lo]  <= ram_re[perm_rev];
                        ram_im[perm_lo]  <= ram_im[perm_rev];
                        ram_re[perm_rev] <= ram_re[perm_lo];
                        ram_im[perm_rev] <= ram_im[perm_lo];
                    end
                    perm_i <= perm_i + 1;
                end else begin
                    stage <= 0; group <= 0; pos <= 0;
                    state <= S_BFLY_RD;
                end
            end
            S_BFLY_RD: begin
                a_re <= ram_re[group + pos];
                a_im <= ram_im[group + pos];
                b_re <= ram_re[group + pos + half_w];
                b_im <= ram_im[group + pos + half_w];
                tw_cos <= rom_cos[rom_idx];
                tw_sin <= rom_sin[rom_idx];
                ia_q <= group[LOG2N_MAX-1:0] + pos[LOG2N_MAX-1:0];
                ib_q <= group[LOG2N_MAX-1:0] + pos[LOG2N_MAX-1:0] + half_w[LOG2N_MAX-1:0];
                state <= S_BFLY_WR;
            end
            S_BFLY_WR: begin
                // twiddle W = (cos, -sin):
                //   tr = rhe(br*cos + bi*sin, 17)
                //   ti = rhe(bi*cos - br*sin, 17)
                tr64 = rhe($signed(b_re) * $signed(tw_cos) + $signed(b_im) * $signed(tw_sin), 17);
                ti64 = rhe($signed(b_im) * $signed(tw_cos) - $signed(b_re) * $signed(tw_sin), 17);
                ram_re[ia_q] <= clamp24(rhe($signed(a_re) + tr64, 1));
                ram_im[ia_q] <= clamp24(rhe($signed(a_im) + ti64, 1));
                ram_re[ib_q] <= clamp24(rhe($signed(a_re) - tr64, 1));
                ram_im[ib_q] <= clamp24(rhe($signed(a_im) - ti64, 1));
                if (pos + 1 < half_w) begin
                    pos <= pos + 1;
                    state <= S_BFLY_RD;
                end else if (group + step_w < n_full) begin
                    group <= group + step_w;
                    pos <= 0;
                    state <= S_BFLY_RD;
                end else if (stage + 1 < nbits) begin
                    stage <= stage + 1;
                    group <= 0; pos <= 0;
                    state <= S_BFLY_RD;
                end else begin
                    state <= S_DONE;
                end
            end
            S_DONE: begin
                done <= 1'b1;
                state <= S_IDLE;
            end
            default: state <= S_IDLE;
            endcase
        end
    end
endmodule
