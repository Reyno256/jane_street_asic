# ASIC Reverse-Engineering Pipeline

Documents the open-source tooling and scripts built in this repo to go
from a raw GDS layout to a labeled, hierarchical Verilog netlist showing
the design's recovered word-level structure (registers, adders,
comparators, ...). Validated end-to-end on `warmup/04_final.gds`
(known-answer practice design) before use on `puzzle.gds`.

## Pipeline overview

```
GDS ──Magic──▶ SPICE ──spice2v.py──▶ structural .v ──Yosys──▶ gate-level JSON
                                                                     │
                                        ┌────────────────────────────┘
                                        ▼
                          structural_recover.py (analysis / report)
                                        │
                                        ▼
                        emit_recovered_verilog.py (labeled, hierarchical .v)
```

A separate branch of the Yosys step also produces a single flattened,
self-contained file for pasting into a Yosys-based online viewer/simulator
like [digitaljs.tilk.eu](https://digitaljs.tilk.eu).

## Tools installed (all self-contained under `tools/` and `.venv/`, no `sudo`
except the one-time `brew install --cask xquartz`)

| Tool | Version | Location | Purpose |
|---|---|---|---|
| **volare** | via pip in `.venv` | `.venv/bin/volare` | Fetches pre-built open-source PDK releases |
| **sky130 PDK** | commit `7519dfb04400f224f140749cda44ee7de6f5e095`, `sky130A` variant | `~/.volare/volare/sky130/versions/<hash>/sky130A/` | LEF, tech LEF, GDS, Magic tech files, behavioral Verilog, etc. Symlinked into `pdk/` |
| **Magic** | 8.3.681 | `tools/magic-install/bin/magic` | GDS → SPICE extraction |
| **Netgen** | 1.5.323 | `tools/netgen-install/bin/netgen` | Netlist format conversion / LVS (its own Verilog *writer* is broken for our use case — see Gotchas) |
| **Yosys** | 0.68 | Homebrew (`yosys`) | Synthesis, flattening, gate-level analysis, JSON netlist export |
| **Icarus Verilog** | 13.0 | Homebrew (`iverilog`) | Local Verilog syntax checking (catches errors before pasting into an online tool) |

Both Magic and Netgen were built from source (`RTimothyEdwards/magic`,
`RTimothyEdwards/netgen` on GitHub) with a custom `--prefix` under `tools/`
to avoid needing `sudo` — see `tools/magic/` and `tools/netgen/` for the
cloned sources.

## Step-by-step

### 1. GDS → SPICE (Magic)

Script: `work/extract_warmup.tcl` (swap the `gds read` path to target
`puzzle.gds` instead of the warmup design).

```
export PDKPATH=~/.volare/volare/sky130/versions/7519dfb04400f224f140749cda44ee7de6f5e095/sky130A
export MAGTYPE=mag
tools/magic-install/bin/magic -dnull -noconsole work/extract_warmup.tcl
```

Key steps inside the script, and why:
- `lef read` the sky130 std-cell LEF (abstract pins + boundary only) **before** `gds read`, so each `sky130_fd_sc_hd__*` cell is pre-defined as an opaque black box.
- `gds noduplicates yes` — **critical**. Without it, Magic *merges* the GDS structure's real transistor geometry into the already-LEF-defined cell instead of skipping it, silently defeating the blackboxing and extracting every standard cell down to individual transistors.
- Top-cell detection: snapshot `cellname list allcells` right after the `lef read` calls (the ~600-cell LEF library), then after `gds read` pick the `cellname list top` entry that's both top-level *and* not in that snapshot. (`gds read`'s Tcl return value is empty; `cellname list top` alone is useless post-LEF-read since it lists ~600 library-macro "top" cells too.)
- `extract all` → `ext2spice` produces the SPICE netlist.

Output: `work/warmup_extract/<topcell>.spice` — clean black-box subckt netlist (no transistor-level content for recognized standard cells).

### 2. SPICE → structural Verilog (`spice2v.py`)

```
python3 work/spice2v.py [--no-stubs] [--drop-filler] <in>.spice <lef>.lef <top_subckt_name> <out>.v
```

Custom converter (not Netgen — see Gotchas). Parses `.subckt`/`X` lines
directly (handles `+` continuations). Pin **directions** come from the LEF
(`DIRECTION INPUT/OUTPUT/INOUT`), not guessed — top-level port direction is
inferred from which pins are driven internally.

- `--drop-filler`: skip instantiating power-only cells (decap/tap — every
  LEF pin is INOUT, zero signal pins, zero logic function). Cut the
  warmup's top-level instance count from 230 to 79.
- `--no-stubs`: omit empty leaf-cell module definitions, for pairing with
  real behavioral models instead (see step 3).

### 3. Synthesis to generic gates (Yosys)

The PDK's own behavioral Verilog (`sky130_fd_sc_hd.v`) **cannot be fed to
Yosys** — it relies on Verilog UDPs (`primitive`/`table`/`endprimitive`),
which Yosys's frontend does not support at all (hard parser error). Since
digitaljs is itself built on Yosys, this affects digitaljs too.

Fix: `work/sky130_fd_sc_hd_behavioral_min.v` — minimal hand-written
`assign`/`always` models (plain gates, no UDPs) for the cell types actually
used. **Every equation was mechanically verified against the real PDK
source** before writing it (either the built-in-primitive composition each
cell's own module body uses, or, for `mux2`/`dfrtp` which route through
real UDPs, the literal UDP truth table) — not recalled from memory. This
file only covers what the warmup needs; extend it the same way (read the
source, don't guess) for cell types `puzzle.gds` uses that aren't here yet.

Two Yosys scripts, same first few passes, different endings:

- `work/yosys_digitaljs.ys` → `write_verilog` a single flattened,
  self-contained file using generic primitives (`$_AND_`/`$_DFFE_*`/etc.,
  re-expanded by Yosys's writer back into plain `assign`/`always` Verilog)
  — this is what you paste into digitaljs. Output:
  `work/warmup_extract/adder_demo.digitaljs_ready.v`.
- `work/yosys_recover.ys` → `write_json` a gate-level netlist for the
  Python analysis in step 4. Output: `work/warmup_extract/adder_demo.recover.json`.

Both run: `read_verilog` (behavioral lib + structural netlist) → `hierarchy -top` → `flatten` → `proc` → `opt_clean` → `techmap` → `opt`.

### 4. Word-level structural recovery (`structural_recover.py`)

```
python3 work/structural_recover.py <netlist.json> <top_module>
```

Prints an analysis report (does **not** write a file — see step 5 for
that). Two off-the-shelf Yosys passes were tried for this and **both
failed**, confirmed by isolated minimal test cases, not just flag mistakes:

- **`shregmap`** (shift-register mapping): built for FPGA SRL16/32
  *resource elimination*, which assumes only the two ends of a chain are
  used and discards intermediate taps. Refuses to convert anything where
  intermediate bits fan out elsewhere — which is every real
  parallel-output shift register, including ours.
- **`extract_fa`** (full/half-adder gate-pattern recognition): reported 17
  matches for what should be an 8-9 bit adder; tracing actual net
  connectivity showed most don't chain into a real carry path — false
  positives from coincidental full-adder-shaped subgraphs inside the
  comparator's compound gates. **Boolean logic optimization does not
  preserve a clean per-block gate boundary or a uniform per-bit pattern in
  general** — structural template matching (Yosys's generic `extract -map`
  included) is fragile once a synthesizer has optimized across the
  original RTL module boundaries.

What worked instead: **pure functional recognition.**
- **Registers**: simple, robust graph traversal — chain `$_DFFE_*`/`$_DFF_*`
  cells where D(stage *i*) is driven directly by Q(stage *i*-1); the chain
  start (external D source) is the LSB.
- **Everything else**: cluster remaining gates into connected components
  by plain net connectivity, then brute-force each component's *complete
  truth table* (feasible up to ~20 input bits) and test it against known
  signatures — equality/inequality-to-constant (single-minterm test),
  AND/OR/XOR-of-all-inputs, buffer/inverter/constant, and a **composed**
  hypothesis specifically added for this design: "output is 1 exactly when
  the arithmetic sum of two-or-more known register groups equals some
  constant" — solved directly from the data (not assumed).
- **Recovering the adder as its own block, not fused into the comparator**:
  even when a component can't be split by connectivity alone (no boundary
  net corresponds to "the sum"), search **every internal net** (not just
  boundary nets) for one that's *literally* equal, across all input
  combinations, to a bit of the hypothesized sum
  (`find_literal_sum_bit_nets`). Where found, trace that net's full
  backward logic cone (`backward_cone`) and that *is* the real adder's
  gates — a genuine structural extraction, not a behavioral guess.

Result on the warmup, recovered purely from the gate netlist:
```
REG group 0: 8-bit shift register (clk, en, rst_n) -- fed by top port B
REG group 1: 8-bit shift register (clk, en, rst_n) -- fed by top port A
ADDER:        8 of 9 real sum bits found as literal wires (bit1 fused away --
              496's bit1 is 0, so only its zero-ness mattered downstream)
COMPARATOR:   S = 1 iff (sum of the two register groups) == 496
```
Exact match to the known ground truth (`00_source.v`'s `adder8` +
`comparator496`) — recovered without reading that file.

### 5. Emit the labeled hierarchical Verilog (`emit_recovered_verilog.py`)

```
python3 work/emit_recovered_verilog.py <netlist.json> <top_module> <out.v>
```

Imports directly from `structural_recover.py`. Produces the actual output
*file* (the analysis script above only prints a report):
- Each register chain → an instance of a small verified `SHIFT_REG_EN`
  module (behavioral — the chain topology is already confirmed exactly).
- Each leftover combinational component → its own named module containing
  the **actual recovered gates**, mechanically regenerated as plain
  `and`/`or`/`xor`/`not` primitives — nothing abstracted away. Split into
  `ADDER_N`/`COMPARATOR_N` where the literal-sum-bit search (step 4)
  succeeds, else one fused `COMB_BLOCK_N` (or `EQ_CHECK_N` if it matched
  the equality-on-sum signature without a clean adder split).
- A top module wiring these together, using the ports/directions
  discovered from the netlist (e.g. which register is fed by which
  top-level input) — not asserted from prior knowledge of the design.

Output on the warmup: `work/warmup_extract/adder_demo.recovered.v`, top
module `adder_demo_recovered`, 4 real blocks: 2×`SHIFT_REG_EN`, `ADDER_0`,
`COMPARATOR_0`. Syntax-checked clean with `iverilog -t null`.

**Verification**: re-synthesizing this output back through Yosys
(`yosys_recover.ys` style) and re-running `structural_recover.py` on it
reproduces the identical result (same gate count, same `sum==496`
finding) — confirming the adder/comparator split is a lossless
re-partition of the same real gates, not a behavioral rewrite.

## Gotchas encountered (don't re-debug these)

- **Netgen's `writenet verilog` segfaults** (SIGSEGV, exit 139) partway
  through writing a netlist that's mostly black-box (zero-internal-device)
  subckts — exactly what LEF-blackboxed extraction produces — silently
  truncating the output file. This is why `spice2v.py` exists instead of
  using Netgen for this step. Netgen itself remains fine for LVS
  (`readnet`/`lvs`), which doesn't touch the crashing writer path — not
  yet used in this pipeline.
- Netgen's `writenet verilog <name>` takes a **cell name**, not a file
  path — it writes `<name>.v` to the current directory.
- Netgen's `quit` command also segfaults in batch mode (separate bug) —
  end Netgen Tcl scripts with `exit 0` instead.
- `$`-containing UDP primitive names (e.g. `udp_dff$NSR`) are legal
  Verilog but not the actual blocker for Yosys — the `primitive` keyword
  itself is simply unsupported by Yosys's frontend, full stop. Renaming
  the `$` doesn't help; UDP-based files can't be fed to Yosys at all.
- Some Verilog tooling (an online netlist viewer, specifically) rejects
  `inout` port declarations outright — hence pulling real directions from
  the LEF instead of declaring every port `inout`.

## Known open question

`puzzle.gds` (the real target) reportedly has internal cell/net names
stripped, unlike the warmup's `04_final.gds`. The name-based `addpath`
cell-recognition trick Magic uses in step 1 (matching GDS structure names
against the PDK's `.mag` library by name) may not work there — geometric/
fingerprint-based cell matching might be needed instead. Not yet
confirmed either way; the plan is to just run this pipeline against it and
see exactly where it breaks, since we don't yet know the truth.
