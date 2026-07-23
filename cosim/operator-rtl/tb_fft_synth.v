// Testbench: the synthesizable FFT engine must be bit-exact to the golden
// vectors (the same ones the Python reference, C core, and sim engine match).
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps
module tb_fft_synth;
    localparam LOG2N = 10, N = 1<<LOG2N;
    reg clk=0, rst=1, start=0;
    wire done;
    reg ld_we=0;
    reg [LOG2N-1:0] io_addr=0;
    reg signed [23:0] ld_re=0, ld_im=0;
    wire signed [23:0] rd_re, rd_im;

    p210_fft_synth #(.LOG2N(LOG2N)) dut(.clk(clk),.rst(rst),.start(start),.done(done),
        .ld_we(ld_we),.io_addr(io_addr),.ld_re(ld_re),.ld_im(ld_im),.rd_re(rd_re),.rd_im(rd_im));
    always #5 clk=~clk;

    reg [23:0] vin_re[0:N-1], vin_im[0:N-1], vexp_re[0:N-1], vexp_im[0:N-1];
    integer i, errors;

    initial begin
        $readmemh("in_re_1024.memh", vin_re);
        $readmemh("in_im_1024.memh", vin_im);
        $readmemh("exp_re_1024.memh", vexp_re);
        $readmemh("exp_im_1024.memh", vexp_im);
        repeat(4) @(negedge clk); rst=0; @(negedge clk);
        // load (bit-reversed internally)
        for (i=0;i<N;i=i+1) begin
            ld_we=1; io_addr=i[LOG2N-1:0]; ld_re=vin_re[i][23:0]; ld_im=vin_im[i][23:0];
            @(negedge clk);
        end
        ld_we=0; @(negedge clk);
        start=1; @(negedge clk); start=0;
        wait(done); @(negedge clk);
        errors=0;
        for (i=0;i<N;i=i+1) begin
            // 2-cycle read latency: addr register + synchronous BRAM read.
            io_addr=i[LOG2N-1:0]; @(negedge clk); @(negedge clk); #1;
            if (rd_re!==$signed(vexp_re[i][23:0]) || rd_im!==$signed(vexp_im[i][23:0])) begin
                if (errors<6) $display(" bin %0d: got (%0d,%0d) want (%0d,%0d)",
                    i, rd_re, rd_im, $signed(vexp_re[i][23:0]), $signed(vexp_im[i][23:0]));
                errors=errors+1;
            end
        end
        if (errors==0) begin $display("P210_FFT_SYNTH_BITEXACT PASS (synthesizable engine == golden, N=%0d)",N); $finish(0); end
        else begin $display("P210_FFT_SYNTH_BITEXACT FAIL (%0d bins)", errors); $fatal(1); end
    end
    initial begin #5000000 $display("TIMEOUT"); $fatal(1); end
endmodule
