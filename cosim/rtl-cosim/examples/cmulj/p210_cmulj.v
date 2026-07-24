// p210_cmulj -- multiply-by-j: out_re[i] = -in_im[i], out_im[i] = in_re[i].
//
// A DELIBERATELY non-FFT block used to test whether the twin's RTL-cosim path is
// genuinely block-agnostic: no twiddle ROM ($readmemh), no arithmetic overflow,
// no bit-reversal. It uses the SAME block-processor interface as
// ../../../operator-rtl/p210_fft_synth.v so the fft_block_shim cadence still
// holds: load N complex samples (ld_we/io_addr/ld_re/ld_im), pulse start, assert
// done, read via io_addr/rd_re/rd_im. 24-bit signed components.
//
// Synthesizable style mirrors p210_fft_synth.v: a synchronous-read RAM per
// component, registered read port (1-cycle addr latency observed as the shim's
// two ticks), an FSM that streams the compute pass in place.
//
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps

// single-port, synchronous-read RAM (same read discipline as tdp_bram's port A)
module cmulj_ram #(parameter WIDTH = 24, parameter AW = 10) (
    input  wire                    clk,
    input  wire                    we,
    input  wire [AW-1:0]           addr,
    input  wire signed [WIDTH-1:0] din,
    output reg  signed [WIDTH-1:0] dout
);
    (* ram_style = "block" *) reg signed [WIDTH-1:0] mem [0:(1<<AW)-1];
    always @(posedge clk) begin
        if (we) mem[addr] <= din;
        dout <= mem[addr];
    end
endmodule

module p210_cmulj #(parameter LOG2N = 10) (
    input  wire                clk,
    input  wire                rst,
    input  wire                start,
    output reg                 done,
    input  wire                ld_we,       // host load (natural order, no bitrev)
    input  wire [LOG2N-1:0]    io_addr,     // natural-order host index
    input  wire signed [23:0]  ld_re, ld_im,
    output wire signed [23:0]  rd_re, rd_im
);
    localparam N = 1 << LOG2N;

    // one synchronous-read RAM for re, one for im; shared addr/we like the FFT
    reg               we;
    reg  [LOG2N-1:0]  addr;
    reg  signed [23:0] din_re, din_im;
    wire signed [23:0] q_re, q_im;
    cmulj_ram #(24, LOG2N) mre (.clk(clk), .we(we), .addr(addr), .din(din_re), .dout(q_re));
    cmulj_ram #(24, LOG2N) mim (.clk(clk), .we(we), .addr(addr), .din(din_im), .dout(q_im));
    assign rd_re = q_re;   // registered read (1-cycle addr latency), same as FFT
    assign rd_im = q_im;

    localparam S_IDLE=0, S_RD=1, S_LAT=2, S_CAP=3, S_WR=4, S_DONE=5;
    reg [2:0]        state;
    reg [LOG2N-1:0]  i;                 // stream index
    reg signed [23:0] cap_re, cap_im;   // latched input at index i

    always @(posedge clk) begin
        we <= 0;
        if (rst) begin state <= S_IDLE; done <= 0; end
        else begin
            case (state)
            S_IDLE: begin
                done <= 0;
                if (ld_we) begin
                    we <= 1; addr <= io_addr; din_re <= ld_re; din_im <= ld_im;
                end else begin
                    addr <= io_addr;              // host read port
                end
                if (start) begin i <= 0; state <= S_RD; end
            end
            S_RD:  begin addr <= i; state <= S_LAT; end     // issue read of index i
            S_LAT: state <= S_CAP;                          // RAM latches addr
            S_CAP: begin                                    // q_re/q_im valid now
                cap_re <= q_re; cap_im <= q_im; state <= S_WR;
            end
            S_WR:  begin                                    // write out_re=-im, out_im=re
                we <= 1; addr <= i;
                din_re <= -cap_im;
                din_im <=  cap_re;
                if (i == (N-1)) state <= S_DONE;             // last index
                else begin i <= i + 1; state <= S_RD; end
            end
            S_DONE: begin
                done <= 1; addr <= io_addr;                 // hand read port back to host
                if (start) state <= S_DONE; else state <= S_IDLE;
            end
            default: state <= S_IDLE;
            endcase
        end
    end
endmodule
