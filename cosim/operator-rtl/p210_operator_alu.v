// P210 operator arithmetic core -- the DSP-consuming datapath, synthesizable.
//
// Registered-in, registered-out combinational core for the two multiply-bearing
// operations of golden-arithmetic v1:
//   - the radix-2 DIT butterfly with an 18-bit Q1.17 twiddle multiply and the
//     round-half-to-even schedule (rhe at the twiddle product, rhe at the /2);
//   - the per-bin complex spectral multiply by a Q1.15 table (rhe at >>15).
//
// This is the module whose DSP48E1 count and Fmax the gap analysis needed
// pinned: the concern was whether the golden twiddle width keeps the datapath
// within the 220-DSP budget of the xc7z020. Unlike the full in-place FFT engine
// (which is written for simulation clarity and needs a proper BRAM datapath for
// synthesis), this arithmetic core maps directly onto DSP48E1s and closes
// timing, so it gives real per-unit resource and Fmax numbers.
//
// Arithmetic is bit-identical to golden.py / the C core / the FFT-engine sim.
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps

module p210_operator_alu (
    input  wire                clk,
    input  wire                rst,
    // butterfly inputs
    input  wire signed [23:0]  a_re, a_im,   // top sample
    input  wire signed [23:0]  b_re, b_im,   // bottom sample
    input  wire signed [17:0]  tw_cos,       // Q1.17 cos
    input  wire signed [17:0]  tw_sin,       // Q1.17 sin (twiddle = cos, -sin)
    // spectral-multiply inputs
    input  wire signed [23:0]  x_re, x_im,   // spectrum bin
    input  wire signed [15:0]  h_re, h_im,   // Q1.15 table mantissa
    // butterfly outputs (a +/- W*b), each /2 rounded
    output reg  signed [23:0]  y0_re, y0_im, // a + W*b, /2
    output reg  signed [23:0]  y1_re, y1_im, // a - W*b, /2
    // spectral-multiply output rhe(x*h, 15)
    output reg  signed [23:0]  m_re, m_im
);
    // round-half-to-even right shift (same function as golden.py / C / engine)
    function signed [63:0] rhe;
        input signed [63:0] v;
        input integer s;
        reg signed [63:0] q, mask, half;
        begin
            if (s == 0) rhe = v;
            else begin
                half = 64'sd1 <<< (s - 1);
                mask = (64'sd1 <<< s) - 1;
                q = (v + half) >>> s;
                if (((v & mask) == half) && q[0]) q = q - 1;
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

    // twiddle product tr,ti = rhe(b * (cos,-sin), 17); butterfly then /2 rounded
    wire signed [63:0] tr = rhe($signed(b_re) * $signed(tw_cos) + $signed(b_im) * $signed(tw_sin), 17);
    wire signed [63:0] ti = rhe($signed(b_im) * $signed(tw_cos) - $signed(b_re) * $signed(tw_sin), 17);
    // spectral multiply rhe(x*h, 15)
    wire signed [63:0] sr = rhe($signed(x_re) * $signed(h_re) - $signed(x_im) * $signed(h_im), 15);
    wire signed [63:0] si = rhe($signed(x_re) * $signed(h_im) + $signed(x_im) * $signed(h_re), 15);

    always @(posedge clk) begin
        if (rst) begin
            y0_re <= 0; y0_im <= 0; y1_re <= 0; y1_im <= 0; m_re <= 0; m_im <= 0;
        end else begin
            y0_re <= clamp24(rhe($signed(a_re) + tr, 1));
            y0_im <= clamp24(rhe($signed(a_im) + ti, 1));
            y1_re <= clamp24(rhe($signed(a_re) - tr, 1));
            y1_im <= clamp24(rhe($signed(a_im) - ti, 1));
            m_re  <= clamp24(sr);
            m_im  <= clamp24(si);
        end
    end
endmodule
