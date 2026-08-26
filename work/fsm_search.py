#!/usr/bin/env python3
"""
Full cycle-based simulator + BFS search over the puzzle FSM's state space,
looking for an input sequence on `I` that drives `success`'s D-input high
(i.e. success will latch to 1 on the following clock edge).

Model: all flip-flops share one clock and one active-low async reset
(confirmed directly from the netlist, not assumed -- see `assert_single_clock_domain`).
The only true free inputs per cycle are `I` and `enable`; VGND/VPWR are
fixed power rails (0/1).

IMPORTANT: every net ID used here (I, enable, VGND, VPWR, success's D-net)
is looked up dynamically from the netlist at load time -- NEVER hardcoded.
Net IDs are not stable across different synthesis runs of the same design
(they shift whenever the source library/Verilog changes and the netlist
gets re-flattened/re-techmapped); a hardcoded net155 from one run silently
meant something else entirely after a since-fixed re-synthesis, producing
a bogus "solution" that looked plausible until cross-checked. Always derive
these fresh from the `ports` dict and from tracing success's own D-input.

Each BFS step: given the current register state plus a chosen (I, enable)
pair, fixpoint-evaluate every combinational gate to get every D input (and
success's own D-net) in one shot -- a full, literal gate-by-gate
simulation (extended with $_MUX_ support, Y = S ? B : A, verified against
Yosys's own simcells.v), not a symbolic shortcut. States are deduplicated
(many different input histories can reach the same state), which is what
makes BFS tractable instead of exponential in depth.

Usage: fsm_search.py <netlist.json> <top_module> [--vary-enable] [--max-depth N] [--max-states N]
"""
import argparse
import sys
from collections import deque

sys.path.insert(0, "work")
from structural_recover import load, build_graphs, DFF_TYPES_PREFIX, COMB_GATES, UNARY_GATES


def build_model(cells):
    dffs = {n: c for n, c in cells.items() if c["type"].startswith(DFF_TYPES_PREFIX)}
    comb = {n: c for n, c in cells.items()
            if c["type"] != "$scopeinfo" and n not in dffs}
    q_nets = [c["connections"]["Q"][0] for c in dffs.values()]
    d_of = {c["connections"]["Q"][0]: c["connections"]["D"][0] for c in dffs.values()}
    # Reset VALUE depends on cell type -- e.g. $_DFF_PN1_ (sky130's dfstp,
    # an async SET, not reset: asserting the shared control net forces
    # Q=1, not Q=0). Blindly initializing every register to 0 (as an
    # earlier version of this file, and the -set-init-zero SAT flag, both
    # did) silently gets these wrong and produces a different, INVALID
    # state trajectory -- caught by cross-checking a real iverilog
    # testbench against this simulator and finding they disagreed on
    # exactly the cycle count and final output, despite an otherwise
    # correct model.
    reset_val_of = {}
    for c in dffs.values():
        q = c["connections"]["Q"][0]
        reset_val_of[q] = 1 if "N1" in c["type"] else 0
    return dffs, comb, q_nets, d_of, reset_val_of


def assert_single_clock_domain(dffs):
    clk_nets = {c["connections"]["C"][0] for c in dffs.values()}
    rst_nets = {c["connections"].get("R", [None])[0] for c in dffs.values() if "R" in c["connections"]}
    if len(clk_nets) != 1:
        raise RuntimeError(f"expected one shared clock net, found {clk_nets} -- "
                            f"this simulator's cycle-based model assumes a single synchronous domain")
    if len(rst_nets) > 1:
        raise RuntimeError(f"expected at most one shared reset net, found {rst_nets}")
    return next(iter(clk_nets))


def eval_step(comb, values):
    """Fixpoint-evaluate every combinational cell given a starting `values`
    dict (register Q's + I/enable/power rails already filled in). Mutates
    and returns `values` with every reachable net's value filled in."""
    pending = list(comb.items())
    progress = True
    while pending and progress:
        progress = False
        still = []
        for cname, c in pending:
            conns = c["connections"]
            ctype = c["type"]
            try:
                if ctype in UNARY_GATES:
                    values[conns["Y"][0]] = UNARY_GATES[ctype](values[conns["A"][0]])
                elif ctype in COMB_GATES:
                    values[conns["Y"][0]] = COMB_GATES[ctype](values[conns["A"][0]], values[conns["B"][0]])
                elif ctype == "$_MUX_":
                    s = values[conns["S"][0]]
                    values[conns["Y"][0]] = values[conns["B"][0]] if s else values[conns["A"][0]]
                else:
                    still.append((cname, c))
                    continue
                progress = True
            except KeyError:
                still.append((cname, c))
        pending = still
    if pending:
        unresolved_types = {c["type"] for _, c in pending}
        raise RuntimeError(f"simulation stalled, unresolved cell types: {unresolved_types} "
                            f"({len(pending)} cells) -- extend eval_step")
    return values


def step(comb, q_nets, d_of, state, i_val, en_val, i_net, en_net, vgnd_net, vpwr_net):
    values = dict(state)
    values[i_net] = i_val
    values[en_net] = en_val
    values[vgnd_net] = 0
    values[vpwr_net] = 1
    eval_step(comb, values)
    new_state = {q: values[d_of[q]] for q in q_nets}
    return new_state, values


def search(cells, ports, vary_enable, max_depth, max_states):
    net_driver, _ = build_graphs(cells)
    dffs, comb, q_nets, d_of, reset_val_of = build_model(cells)
    assert_single_clock_domain(dffs)
    q_nets = sorted(q_nets)

    i_net = ports["I"]["bits"][0]
    en_net = ports["enable"]["bits"][0]
    vgnd_net = ports["VGND"]["bits"][0]
    vpwr_net = ports["VPWR"]["bits"][0]
    success_q_net = ports["success"]["bits"][0]
    success_drv = net_driver[success_q_net]
    success_d_net = cells[success_drv[0]]["connections"]["D"][0]
    print(f"resolved: I=net{i_net} enable=net{en_net} VGND=net{vgnd_net} VPWR=net{vpwr_net} "
          f"success_D=net{success_d_net}", file=sys.stderr)
    non_zero_reset = {q: v for q, v in reset_val_of.items() if v != 0}
    if non_zero_reset:
        print(f"registers with non-zero reset value: {non_zero_reset}", file=sys.stderr)

    initial = {q: reset_val_of[q] for q in q_nets}
    start_key = tuple(initial[q] for q in q_nets)
    visited = {start_key}
    queue = deque([(initial, [])])
    en_choices = (0, 1) if vary_enable else (1,)

    states_explored = 0
    while queue:
        state, path = queue.popleft()
        if len(path) >= max_depth:
            continue
        for en_val in en_choices:
            for i_val in (0, 1):
                new_state, values = step(comb, q_nets, d_of, state, i_val, en_val,
                                          i_net, en_net, vgnd_net, vpwr_net)
                states_explored += 1
                if values[success_d_net] == 1:
                    return path + [(i_val, en_val)], states_explored
                key = tuple(new_state[q] for q in q_nets)
                if key not in visited:
                    visited.add(key)
                    queue.append((new_state, path + [(i_val, en_val)]))
        if states_explored >= max_states:
            print(f"  ... hit max_states={max_states} cap, {len(visited)} distinct states seen, "
                  f"still searching depth {len(path)}", file=sys.stderr)
            return None, states_explored
    return None, states_explored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("top_module")
    ap.add_argument("--vary-enable", action="store_true",
                     help="also search over enable=0/1 per cycle (default: enable fixed to 1)")
    ap.add_argument("--max-depth", type=int, default=200)
    ap.add_argument("--max-states", type=int, default=2_000_000)
    args = ap.parse_args()

    cells, ports = load(args.json_path, args.top_module)
    result, n = search(cells, ports, args.vary_enable, args.max_depth, args.max_states)
    print(f"states explored: {n}")
    if result is None:
        print("NO SOLUTION FOUND within limits")
        sys.exit(1)
    print(f"SOLUTION FOUND, length {len(result)} cycles:")
    i_seq = "".join(str(i) for i, e in result)
    en_seq = "".join(str(e) for i, e in result)
    print(f"  I sequence (one bit per cycle, in order): {i_seq}")
    if args.vary_enable:
        print(f"  enable sequence:                         {en_seq}")


if __name__ == "__main__":
    main()
