#!/bin/bash
# Corrected version of sat_search.sh: uses gen_sat_script.py (real rst_n
# reset cycle + dfxtp forced via -set-init) instead of -set-init-zero.
set -e
MIN_N=${1:-1}
MAX_N=${2:-123}
cd /Users/quinn/Desktop/asic-puzzle-2026-master

for N in $(seq "$MIN_N" "$MAX_N"); do
  echo "=== trying N=$N ===" >&2
  python3 work/gen_sat_script.py "$N" > work/yosys_sat_search.ys
  OUT=$(yosys -Q -s work/yosys_sat_search.ys 2>&1)
  if echo "$OUT" | grep -q "SAT solving finished - model found"; then
    echo "SAT at N=$N" >&2
    echo "$OUT" > work/puzzle_extract/sat2_result_N${N}.txt
    echo "$OUT" | grep -A 500 "Signal Name"
    exit 0
  fi
done
echo "UNSAT for all N in [$MIN_N, $MAX_N]" >&2
exit 1
