// Testbench: prove the RTL FFT engine is bit-exact to the golden pinned
// vectors (the same vectors the Python reference and the C core match).
// Runs N=256 (seed 11) and N=1024 (seed 12); every output word must equal the
// golden expectation exactly. SPDX-License-Identifier: MIT
`timescale 1ns/1ps

module tb_fft_engine;
    localparam LOG2N_MAX = 12;
    localparam NMAX = 1 << LOG2N_MAX;

    reg clk = 0, rst = 1, start = 0;
    reg [4:0] log2n;
    wire done;
    reg mem_we = 0;
    reg [LOG2N_MAX-1:0] mem_addr = 0;
    reg signed [23:0] mem_wr_re = 0, mem_wr_im = 0;
    wire signed [23:0] mem_rd_re, mem_rd_im;

    p210_fft_engine #(.LOG2N_MAX(LOG2N_MAX)) dut (
        .clk(clk), .rst(rst), .start(start), .log2n(log2n), .done(done),
        .mem_we(mem_we), .mem_addr(mem_addr),
        .mem_wr_re(mem_wr_re), .mem_wr_im(mem_wr_im),
        .mem_rd_re(mem_rd_re), .mem_rd_im(mem_rd_im)
    );

    always #5 clk = ~clk;

    reg [23:0] vin_re [0:NMAX-1];
    reg [23:0] vin_im [0:NMAX-1];
    reg [23:0] vexp_re [0:NMAX-1];
    reg [23:0] vexp_im [0:NMAX-1];

    integer errors;
    integer i;
    integer total_errors = 0;

    task run_case(input [4:0] bits, input [1023:0] fre, input [1023:0] fim,
                  input [1023:0] fere, input [1023:0] feim);
        integer n;
        begin
            n = 1 << bits;
            $readmemh(fre, vin_re);
            $readmemh(fim, vin_im);
            $readmemh(fere, vexp_re);
            $readmemh(feim, vexp_im);
            // load
            @(negedge clk);
            for (i = 0; i < n; i = i + 1) begin
                mem_we = 1; mem_addr = i[LOG2N_MAX-1:0];
                mem_wr_re = vin_re[i][23:0]; mem_wr_im = vin_im[i][23:0];
                @(negedge clk);
            end
            mem_we = 0;
            // run
            log2n = bits; start = 1; @(negedge clk); start = 0;
            wait (done); @(negedge clk);
            // compare
            errors = 0;
            for (i = 0; i < n; i = i + 1) begin
                mem_addr = i[LOG2N_MAX-1:0];
                #1;
                if (mem_rd_re !== $signed(vexp_re[i][23:0]) ||
                    mem_rd_im !== $signed(vexp_im[i][23:0])) begin
                    if (errors < 5)
                        $display("  bin %0d: got (%0d,%0d) want (%0d,%0d)",
                                 i, mem_rd_re, mem_rd_im,
                                 $signed(vexp_re[i][23:0]), $signed(vexp_im[i][23:0]));
                    errors = errors + 1;
                end
            end
            if (errors == 0)
                $display("N=%0d: MATCH (all %0d bins bit-exact)", n, n);
            else begin
                $display("N=%0d: DIVERGED (%0d bins)", n, errors);
                total_errors = total_errors + errors;
            end
        end
    endtask


    initial begin
        repeat (4) @(negedge clk);
        rst = 0;
        @(negedge clk);
        run_case(5'd8,  "in_re_256.memh",  "in_im_256.memh",
                        "exp_re_256.memh", "exp_im_256.memh");
        run_case(5'd10, "in_re_1024.memh", "in_im_1024.memh",
                        "exp_re_1024.memh", "exp_im_1024.memh");
        if (total_errors == 0) begin
            $display("P210_FFT_RTL_BITEXACT PASS (RTL == golden)");
            $finish(0);
        end else begin
            $display("P210_FFT_RTL_BITEXACT FAIL");
            $fatal(1);
        end
    end
endmodule
