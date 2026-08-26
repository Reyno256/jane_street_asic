#!/bin/bash
# Loop increasing N, asking Yosys's SAT solver "is there an I/enable trace
# where success=1 at exactly cycle N", stopping at the first N that's SAT.
# This finds the EARLIEST cycle success can go high (checking each N in
# order matters because success is only partially sticky -- it can revert
# to 0 on a later cycle if its latch-hold condition fails -- so we can't
# just pick one large N and assume an earlier solution would show up in
# the same trace).
set -e
MIN_N=${1:-1}
MAX_N=${2:-80}
cd /Users/quinn/Desktop/asic-puzzle-2026-master

for N in $(seq "$MIN_N" "$MAX_N"); do
  echo "=== trying N=$N ===" >&2
  cat > work/yosys_sat_search.ys << EOF
read_verilog work/sky130_fd_sc_hd_behavioral_puzzle.v
read_verilog work/puzzle_extract/puzzle.digitaljs.v
hierarchy -top puzzle
flatten
proc
opt_clean
techmap
opt
async2sync
opt_clean

sat -seq $N -set-init-zero -set-at $N success 1 -show-inputs -show I -show enable -show success -dump_vcd work/puzzle_extract/sat_solution.vcd
EOF
  OUT=$(yosys -Q -s work/yosys_sat_search.ys 2>&1)
  if echo "$OUT" | grep -q "SAT solving finished - model found"; then
    echo "SAT at N=$N" >&2
    echo "$OUT" > work/puzzle_extract/sat_result_N${N}.txt
    echo "$OUT" | grep -A 200 "Signal Name"
    exit 0
  fi
done
echo "UNSAT for all N in [$MIN_N, $MAX_N]" >&2
exit 1
