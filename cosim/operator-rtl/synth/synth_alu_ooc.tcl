# Out-of-context synthesis of the operator arithmetic core on the P210 part.
# Reports real DSP48E1 / LUT / FF utilization and timing. Run on a native-x86_64
# Vivado (see RESULTS.md for the emulation caveat on aarch64/Rosetta hosts).
#   vivado -mode batch -source synth_alu_ooc.tcl
set part xc7z020clg400-1
read_verilog [file join [file dirname [info script]] .. p210_operator_alu.v]
synth_design -top p210_operator_alu -part $part -mode out_of_context
create_clock -name clk -period 4.000 [get_ports clk]
report_utilization -file alu_util.rpt
report_timing_summary -file alu_timing.rpt
set wns [get_property SLACK [get_timing_paths -max_paths 1 -setup]]
puts "ALU_SYNTH part=$part target_ns=4.000 wns_ns=$wns fmax_mhz=[expr {1000.0/(4.000-$wns)}]"
exit
