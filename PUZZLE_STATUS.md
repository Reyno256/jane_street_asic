# puzzle.gds — Investigation Status

Summary of the reverse-engineering work on the real puzzle (`puzzle.gds`),
saved as a checkpoint before continuing. See `PIPELINE.md` for the
general tooling (Magic/Yosys/SAT pipeline) this all runs on.

## What's been established (high confidence)

**Extraction & synthesis**: Cell/net names in `puzzle.gds` are NOT
stripped (corrected an earlier assumption). Magic extraction
(`work/extract_puzzle.tcl`, LEF-blackboxed, same technique as the
warmup) worked directly: 70 unique cell types, 1618 gate instances, top
cell literally named `puzzle`. Behavioral library extended from 16 to 67
cell types (`work/sky130_fd_sc_hd_behavioral_puzzle.v`), 50 auto-generated
via `work/gen_cell_behaviors.py` and mechanically validated against
hand-derived warmup cells before trusting it at scale. Synthesizes to
~2063 gate-level cells, 92 flip-flops, top ports: `I`, `O[7:0]`, `enable`,
`success`, `clk`, `rst_n`.

**Structure**: Two regions.
1. A **success-detection FSM**: a 12-bit shift buffer for serial input
   `I`, feeding a 72-bit state register (71 of 72 bits show genuine
   closed-loop state↔next-state feedback — the structural signature of an
   FSM, confirmed via a 1187-gate AND/OR/NOT-dominated decode cloud, only
   41 XOR gates). `success` is literally a flip-flop's Q output; its
   D-input is `(success & latch-hold-term) | (a single ~57-literal AND
   term)` — i.e. one specific required state pattern, extracted directly
   via sum-of-products reading (`work/extract_sop.py`) rather than
   brute-force (the state bits don't form a simple shift-register history
   of `I`, so the required pattern is a real *state*, not a readable
   input sequence).
2. An **output generator**: ~386 gates, 26 inputs (including `success`
   itself as one input) → `O[7:0]` plus a few internal nets. Matches the
   README's "output generator" hint.

## What's been found to be WRONG (real bugs caught along the way)

These are documented in detail in memory (`project_asic_puzzle_2026.md`)
but the short version, because they matter for anyone continuing this work:

1. **Silent data loss in `gen_cell_behaviors.py`**: `dfxtp_2`/`dfstp_2`
   auto-generated with a missing flip-flop instantiation (a `` `UNIT_DELAY ``
   macro token broke the line parser, which silently skipped the line
   instead of erroring). Fixed: parser now fails loudly on any
   unrecognized line; both cells hand-written correctly afterward.

2. **Wrong reset value for `dfstp_2`** (4 of 92 registers): this is an
   async **SET** flip-flop (`SET_B` tied directly to the same `rst_n` net
   used for RESET elsewhere) — asserting reset forces these 4 to **1**,
   not 0. Both the custom Python simulator (`work/fsm_search.py`) and the
   first SAT search blindly initialized every register to 0. Partially
   fixed in `fsm_search.py` (`build_model` now returns a proper
   `reset_val_of` dict, keyed off `"N1" in cell type`), but see next point.

3. **Stale hardcoded net ID**: an early version of `fsm_search.py`
   hardcoded `SUCCESS_D_NET = 155` from one synthesis run; net IDs shift
   across re-synthesis (confirmed: became 171 after fix #1 above). Fixed:
   all such net IDs are now resolved dynamically from `ports`/`net_driver`
   at call time, never hardcoded.

4. **A real gate-level testbench (`work/tb_puzzle.v`) was built** to
   cross-check the Python/SAT results against direct simulation of the
   actual extracted netlist — and even after fix #2, it still disagrees
   with the Python model (testbench: `success` at cycle 122, `O=42`;
   Python: cycle 123, `O=20`, for the identical 123-bit input sequence).
   **This remaining discrepancy is not yet root-caused.** Likely another
   reset/initialization mismatch (possibly `-set-init-zero` in the SAT
   flow not actually reaching every flip-flop, or a genuine difference
   introduced by Yosys's `flatten/opt/techmap` passes vs. the raw
   structural netlist iverilog simulates directly).

## Current best-known answer (NOT fully trusted, testbench-verified for this specific sequence)

The 123-bit `I` sequence originally found via SAT (from the *buggy*
pre-fix model) was re-run on the trustworthy gate-level testbench
(`work/tb_puzzle.v`, real async-reset triggering confirmed correct) and
does genuinely work:

- `I` sequence: `000000010101000010000000000001010101000000000000101000000100000100000010000010100001000000010000001000001001000101000000001`
- `enable`: 1 for cycles 1–121, then 0 for the last two.
- Confirmed via testbench: **`success` is already 1 by cycle 122** (one
  cycle earlier than the sequence was designed for), and `O[7:0] =
  00101010` (decimal **42**) once the full 123-bit sequence completes.

This is real and testbench-confirmed for *this specific sequence*, but
since it was derived from a buggy model, **it's very unlikely to be the
minimal or "intended" sequence** — the true minimum could be ≤121 cycles,
or a different sequence entirely. The testbench itself is trustworthy;
the sequence that was fed into it is a leftover from before the bugs
above were found.

## RESOLVED — corrected search complete, current best answer

Redid the SAT search driving the design's real `rst_n` port through an
actual reset step (`-set-at 1 rst_n 0`, then pinned to 1 for every
subsequent step) instead of fighting Yosys's post-optimization signal
renaming for manual `-set-init` (that direct approach failed for `dfstp_2`
specifically: "Found -set-init bits that are not part of the
initial_state" — even without the `opt` pass; `-set-init` does work for
`dfxtp_2`, just not `dfstp_2`, likely because `async2sync`'s conversion of
an async-**SET** cell restructures its output net in a way that isn't
tracked as a plain flop's initial state).

**New bug found and understood**: `async2sync` converts the async reset
into a *synchronous* one, so the dedicated reset step in the SAT model
consumes a real clock edge — but actual async hardware (and the
testbench, which asserts `rst_n` before any clock edge occurs) needs
*zero* edges for reset. This makes the SAT model **systematically
overestimate the required cycle count by exactly 1** (a constant,
sequence-independent bias, so it doesn't affect the correctness of
*which* sequence is minimal, only the reported length). Confirmed
directly: the bisected corrected-model minimum was still N=123, but the
testbench's own per-cycle check already showed `success` going high at
cycle 122 using just the first 122 bits of that sequence — proving the
extra cycle was spurious.

**Final, testbench-verified answer** (`work/tb_puzzle.v`, current state):
- `I` sequence (122 bits): `00000001010100001000000000000101010100000000000010100000010000010000001000001010000100000001000000100000100100010100000000`
- `enable`: 1 for all 122 cycles except the last, which is 0.
- Result: `success=1` at **exactly cycle 122** (no earlier, no later — a
  clean, minimal fit), `O[7:0] = 00101000` = decimal **40**.

This is now believed to be the true minimum (not just minimal for this
one candidate sequence) because the async2sync reset bias is a constant
+1 applied uniformly to every candidate the SAT search considers, so the
bisected minimum (123, before correction) reliably corresponds to the
true global minimum (122, after correction) — the search itself covered
the full space correctly, only the reported cycle count needed adjusting.

**UPDATE — the answer was cross-checked against `example_inputs.vcd` (read with explicit user permission) and confirmed correct, with a bonus discovery: `O` is not a single numeric byte at all.**

`example_inputs.vcd`'s (deliberately wrong) input sequence causes the design to stream a null-terminated ASCII message out over `O`, one character per cycle, once `enable` drops: `"TRY AGAIN"`. Re-running the verified 122-cycle sequence the same way (extending the simulation past cycle 122, decoding every subsequent `O` value as ASCII) produces: **`(* TWO STARS *)`** — a complete, well-formed message (literally valid Verilog attribute-comment syntax), strong independent confirmation the sequence is genuinely correct (a wrong guess produces different, but equally coherent, text — not garbage either way, so the *content* of the message, not just its well-formedness, is what distinguishes success). This fully resolves an earlier tangent about whether the "real" output was the byte value 40 or 42 (a coincidental Hitchhiker's-Guide-shaped red herring) — both were just the first two characters of this longer string.

## `puzzle.recovered.v` — hierarchical/labeled Verilog, now complete and independently verified

Built via `work/emit_recovered_verilog.py` against the full `puzzle.recover.json` (not just the post-FSM "rest"). This required fixing three real bugs in the emitter itself, found via a rigorous debugging process worth recording since the failure mode (silent X in simulation, only at one specific critical cycle) is exactly the kind of thing that looks like a deep logic bug but isn't:

1. **Standalone (non-chained) registers were never instantiated at all** — `structural_recover.py`'s `reg_singles` were correctly excluded from combinational clustering but the emitter's top-module loop only iterated over chain members, silently dropping 80 of 92 registers as undriven floating wires (syntactically legal Verilog, so `iverilog` never complained). Fixed by adding one small primitive module per DFF type actually present (`DFF_PN0`, `DFF_PN1`, `DFF_P` — semantics for each already verified from source earlier in this project) and instantiating every standalone register against the matching one.
2. **`$_MUX_` cells had no Verilog emission path at all** (crashed with `ValueError` immediately) — fixed with `assign Y = S ? B : A` (Yosys's own `simcells.v` semantics, verified, not assumed).
3. **The real bug behind the mysterious X-at-cycle-122 failure**: `SHIFT_REG_EN` hardcoded an 8-bit-wide shift register, copied from the warmup (which genuinely has 8-bit chains) without generalizing it — the puzzle's actual chain is 12 bits. Verilog silently resolves out-of-range bit selects (`reg0_q[8]` through `[11]` on an 8-bit vector) to `x` rather than erroring, and those specific bits only became load-bearing at the exact cycle the FSM's 52-bit match condition needed them — explaining why the design worked correctly for 121 cycles and only broke at the trigger. Diagnosed by cross-checking the emitted file's `success` signal against two independent engines (the original `puzzle.digitaljs.v` netlist and the from-scratch Python simulator), both of which gave clean, defined answers, proving the *design* was fine and the bug was specifically in the *emission*; the actual smoking gun was `iverilog -Wall`'s `"Constant bit select [10] is after vector reg0_q[7:0]"` warning — worth remembering to always compile with `-Wall` when debugging mysterious X-propagation, rather than just checking for hard errors. Fixed by parameterizing `SHIFT_REG_EN` by width (now named `SHIFT_REG_EN_<width>`).
4. **A second, related naming bug** found by the same `-Wall` pass (`"implicit definition of wire 'net177'"`): `group_control_signals()` (in `structural_recover.py`) has its own independent net-naming fallback (`f"net{b}"`) that doesn't match `emit_recovered_verilog.py`'s `net_name()` convention (`f"w{b}"`) — harmless when a chain's control signal is a raw top-level port (falls back to the real port name either way), but silently created a disconnected, undriven duplicate wire whenever a control signal is itself derived/gated logic (the puzzle's shift-chain enable is `AND(some_signal, top-level enable)`, not the raw port). Fixed by resolving chain control nets through the same `input_source()`/`net_name()` path used everywhere else in the emitter, rather than through `group_control_signals()`'s own naming.

**Final state**: `work/puzzle_extract/puzzle.recovered.v`, top module `puzzle_recovered`, 3550 lines, 7 module definitions (`SHIFT_REG_EN_12`, `DFF_PN0`, `DFF_PN1`, `DFF_P`, `COMB_BLOCK_0`, `COMB_BLOCK_1`, top), 80 standalone register instances + 1 shift-chain instance + 2 combinational-block instances. Independently verified: feeding it the exact same 122-bit sequence (with the same reset procedure as `tb_puzzle.v`) reproduces `success=1` at cycle 122 and the message `(* TWO STARS *)` exactly, with zero `-Wall` warnings beyond cosmetic timescale-inheritance notices. `work/puzzle_extract/puzzle.rest.json` was also regenerated against the corrected library (458 cells in the non-FSM remainder, up from 395 pre-fix — the output-generator picture in the earlier findings section above is now based on stale data and should be treated as superseded by this regeneration, though the qualitative shape — a wide, hard-to-brute-force block with `success` as one of its inputs — held up).
