#!/usr/bin/env python3
"""Generate a Yosys .ys script for the corrected SAT search: one reset
timestep (rst_n=0, forcing every flip-flop -- including dfstp's real
async-SET-to-1 behavior, via the design's own real reset path through
async2sync) followed by N sequence timesteps with rst_n pinned to 1
throughout (so the solver can't "cheat" by re-asserting reset
mid-sequence, matching exactly what the testbench does). The 4 `dfxtp_2`
registers (no reset pin at all) are pinned to 0 via -set-init, matching
the testbench's explicit force (validated: -set-init works for these,
unlike the dfstp/dfrtp cells which are restructured by async2sync).

Usage: gen_sat_script.py <N> > out.ys
"""
import sys

N = int(sys.argv[1])
total_steps = N + 1  # step 1 = reset, steps 2..N+1 = the N sequence cycles

lines = [
    "read_verilog work/sky130_fd_sc_hd_behavioral_puzzle.v",
    "read_verilog work/puzzle_extract/puzzle.digitaljs.v",
    "hierarchy -top puzzle",
    "flatten",
    "proc",
    "opt_clean",
    "techmap",
    "opt",
    "async2sync",
    "opt_clean",
    "",
]

sat_args = [f"-seq {total_steps}"]
sat_args.append("-set-at 1 rst_n 0")
for t in range(2, total_steps + 1):
    sat_args.append(f"-set-at {t} rst_n 1")
for i in range(4):
    sat_args.append(f"-set-init \\sky130_fd_sc_hd__dfxtp_2_{i}.Q 0")
sat_args.append(f"-set-at {total_steps} success 1")
sat_args.append("-show I")
sat_args.append("-show enable")

lines.append("sat " + " ".join(sat_args))
print("\n".join(lines))
