#!/usr/bin/env python3
"""
Recover word-level structure (registers, and other common blocks like
adders/comparators) from a flattened gate-level Yosys JSON netlist.

Method:
- Registers: Yosys's own opt_dff already merges "hold-vs-shift mux + DFF"
  into one $_DFFE_* cell per bit -- no extra recognition needed. A shift
  register is just a chain of these where D(stage i) is directly driven by
  Q(stage i-1); this is found by graph traversal, which is robust and
  doesn't discard the ability to tap every bit (unlike Yosys's `shregmap`,
  which is built for FPGA SRL *resource elimination* and assumes only the
  chain's ends are used -- confirmed by testing it on a bare shift
  register with no other logic, and it still converts nothing).
- Everything else: an earlier attempt used Yosys's `extract_fa` (full/half
  adder gate-pattern recognition) to find the adder, but that turned out
  to be unreliable here -- it reported ~2x as many full-adder matches as
  an 8-bit adder needs, and tracing the actual net connectivity showed
  those matches don't chain into a real carry path (false positives,
  likely coincidental full-adder-shaped subgraphs inside the comparator's
  compound AOI/OAI gates -- boolean-minimized logic doesn't preserve a
  clean per-block gate boundary in general).
  Instead this uses purely FUNCTIONAL recognition: cluster the remaining
  gates into connected components, identify which known register groups
  feed each component's primary inputs, brute-force the component's full
  truth table, and test it against boolean *signatures* of common
  functions -- including the *composed* hypothesis "output is 1 exactly
  when (groupA + groupB) equals some constant", solving for that constant
  directly from the data. This works even when the adder and comparator
  logic are gate-level intertwined with no clean structural cut point,
  which is exactly what was observed here.

Usage: structural_recover.py <netlist.json> <top_module_name>
"""
import itertools
import json
import sys
from collections import defaultdict

COMB_GATES = {
    "$_AND_": lambda a, b: a & b,
    "$_OR_": lambda a, b: a | b,
    "$_XOR_": lambda a, b: a ^ b,
    "$_NAND_": lambda a, b: 1 - (a & b),
    "$_NOR_": lambda a, b: 1 - (a | b),
    "$_XNOR_": lambda a, b: 1 - (a ^ b),
}
UNARY_GATES = {
    "$_NOT_": lambda a: 1 - a,
    "$_BUF_": lambda a: a,
}
DFF_TYPES_PREFIX = ("$_DFF_", "$_DFFE_", "$_SDFF_", "$_SDFFE_", "$_ADFF_", "$_ADFFE_")
MAX_BRUTE_FORCE_INPUTS = 20


def load(json_path, top):
    with open(json_path) as f:
        data = json.load(f)
    mod = data["modules"][top]
    cells = {name: c for name, c in mod["cells"].items() if c["type"] != "$scopeinfo"}
    ports = mod["ports"]
    return cells, ports


def build_graphs(cells):
    net_driver = {}
    net_consumers = defaultdict(list)
    for cname, c in cells.items():
        pd = c["port_directions"]
        for pname, bits in c["connections"].items():
            direction = pd.get(pname)
            for b in bits:
                if isinstance(b, str):
                    continue
                if direction == "output":
                    net_driver[b] = (cname, pname)
                elif direction == "input":
                    net_consumers[b].append((cname, pname))
    return net_driver, net_consumers


def bit_to_port_name(ports, bit):
    for pname, p in ports.items():
        for i, b in enumerate(p["bits"]):
            if b == bit:
                suffix = f"[{i}]" if len(p["bits"]) > 1 else ""
                return f"{pname}{suffix}"
    return None


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self):
        g = defaultdict(list)
        for x in self.parent:
            g[self.find(x)].append(x)
        return list(g.values())


def find_register_chains(cells, net_driver):
    dff_cells = {n: c for n, c in cells.items() if c["type"].startswith(DFF_TYPES_PREFIX)}
    uf = UnionFind(dff_cells)
    next_in_chain = {}  # driver dff -> dff whose D it feeds (chain successor)
    for n, c in dff_cells.items():
        d_bits = c["connections"].get("D", [])
        if not d_bits or isinstance(d_bits[0], str):
            continue
        drv = net_driver.get(d_bits[0])
        if drv and drv[0] in dff_cells and drv[1] == "Q":
            uf.union(n, drv[0])
            next_in_chain[drv[0]] = n

    chains, singles = [], []
    for members in uf.groups():
        if len(members) == 1:
            singles.append(members[0])
            continue
        # order the chain: start = the member with no predecessor in-group
        preds = {v: k for k, v in next_in_chain.items() if k in members and v in members}
        start = next(m for m in members if m not in preds)
        ordered = [start]
        cur = start
        while cur in next_in_chain and next_in_chain[cur] in members:
            cur = next_in_chain[cur]
            ordered.append(cur)
        chains.append(ordered)
    return chains, singles


def group_control_signals(cells, ports, members):
    ctrl = {}
    for port_letter in ("C", "E", "R", "S"):
        nets = set()
        for m in members:
            bits = cells[m]["connections"].get(port_letter)
            if bits and not isinstance(bits[0], str):
                nets.add(bits[0])
        if nets:
            names = {bit_to_port_name(ports, b) or f"net{b}" for b in nets}
            ctrl[port_letter] = names
    return ctrl


def leftover_components(cells, net_consumers, classified_cells):
    leftover = {n: c for n, c in cells.items() if n not in classified_cells}
    uf = UnionFind(leftover)
    for n, c in leftover.items():
        for pname, bits in c["connections"].items():
            if c["port_directions"].get(pname) != "output":
                continue
            for b in bits:
                if isinstance(b, str):
                    continue
                for cons_cell, _ in net_consumers.get(b, []):
                    if cons_cell in leftover:
                        uf.union(n, cons_cell)
    return uf.groups(), leftover


def component_boundary(cells, comp_members, net_driver, net_consumers):
    member_set = set(comp_members)
    inputs, outputs = set(), set()
    for n in comp_members:
        c = cells[n]
        for pname, bits in c["connections"].items():
            direction = c["port_directions"].get(pname)
            for b in bits:
                if isinstance(b, str):
                    continue
                if direction == "input":
                    drv = net_driver.get(b)
                    if drv is None or drv[0] not in member_set:
                        inputs.add(b)
                elif direction == "output":
                    consumers = net_consumers.get(b, [])
                    if not consumers or any(cc not in member_set for cc, _ in consumers):
                        outputs.add(b)
    return sorted(inputs), sorted(outputs)


def simulate_component(cells, comp_members, input_bits, output_bits):
    """Brute-force truth table: list of (combo_tuple, {output_bit: value})."""
    table = []
    for combo in itertools.product([0, 1], repeat=len(input_bits)):
        values = dict(zip(input_bits, combo))
        pending = list(comp_members)
        progress = True
        while pending and progress:
            progress = False
            still_pending = []
            for cname in pending:
                c = cells[cname]
                conns = c["connections"]
                ctype = c["type"]
                try:
                    if ctype in UNARY_GATES:
                        a = values[conns["A"][0]]
                        values[conns["Y"][0]] = UNARY_GATES[ctype](a)
                    elif ctype in COMB_GATES:
                        a = values[conns["A"][0]]
                        b = values[conns["B"][0]]
                        values[conns["Y"][0]] = COMB_GATES[ctype](a, b)
                    else:
                        still_pending.append(cname)
                        continue
                    progress = True
                except KeyError:
                    still_pending.append(cname)
            pending = still_pending
        if any(ob not in values for ob in output_bits):
            return None
        table.append((combo, {ob: values[ob] for ob in output_bits}))
    return table


def simulate_component_full(cells, comp_members, input_bits):
    """Like simulate_component, but returns every internal net's value for
    every input combo (not just the declared output bits) -- needed to find
    literal internal wires matching some target function (e.g. a sum bit)
    that never happens to be a component *boundary* net."""
    table = []
    for combo in itertools.product([0, 1], repeat=len(input_bits)):
        values = dict(zip(input_bits, combo))
        pending = list(comp_members)
        progress = True
        while pending and progress:
            progress = False
            still_pending = []
            for cname in pending:
                c = cells[cname]
                conns = c["connections"]
                ctype = c["type"]
                try:
                    if ctype in UNARY_GATES:
                        values[conns["Y"][0]] = UNARY_GATES[ctype](values[conns["A"][0]])
                    elif ctype in COMB_GATES:
                        values[conns["Y"][0]] = COMB_GATES[ctype](values[conns["A"][0]], values[conns["B"][0]])
                    else:
                        still_pending.append(cname)
                        continue
                    progress = True
                except KeyError:
                    still_pending.append(cname)
            pending = still_pending
        table.append((combo, values))
    return table


def find_literal_sum_bit_nets(cells, comp_members, in_bits, involved_groups, groups, group_of_bit, max_bit=10):
    """Search every internal net in the component for one that is LITERALLY
    equal (for every input combination) to some bit of the sum of the given
    register groups. Returns {bit_index: net_id} for whichever bits are
    found as real wires (not all bits necessarily exist standalone -- some
    may have been optimized away/fused with downstream logic)."""
    full_table = simulate_component_full(cells, comp_members, in_bits)
    idx_of = {b: i for i, b in enumerate(in_bits)}

    sums = []
    for combo, values in full_table:
        total = 0
        for gid in involved_groups:
            bit_list = [b for b in groups[gid] if b in idx_of]
            total += bits_to_int([combo[idx_of[b]] for b in bit_list])
        sums.append(total)

    all_nets = set()
    for m in comp_members:
        for bits in cells[m]["connections"].values():
            for b in bits:
                if not isinstance(b, str):
                    all_nets.add(b)

    found = {}
    for net in all_nets:
        vals = [values.get(net) for _, values in full_table]
        if any(v is None for v in vals):
            continue
        for bitpos in range(max_bit):
            if all(((sums[i] >> bitpos) & 1) == vals[i] for i in range(len(vals))):
                found.setdefault(bitpos, net)  # keep first match per bit
                break
    return found


def backward_cone(cells, target_nets, comp_members, net_driver):
    """All cells (within comp_members) that transitively drive any of the
    given target nets, stopping at the component's own primary inputs."""
    member_set = set(comp_members)
    cone = set()
    stack = list(target_nets)
    seen_nets = set()
    while stack:
        net = stack.pop()
        if net in seen_nets:
            continue
        seen_nets.add(net)
        drv = net_driver.get(net)
        if drv is None or drv[0] not in member_set:
            continue  # driven from outside the component (a primary input)
        cell_name = drv[0]
        if cell_name not in cone:
            cone.add(cell_name)
            c = cells[cell_name]
            for pname, bits in c["connections"].items():
                if c["port_directions"].get(pname) == "input":
                    stack.extend(b for b in bits if not isinstance(b, str))
    return cone


def classify_single_bit(table, input_bits, output_bit, ports):
    """Generic bitwise-function signatures for a single output bit."""
    vals = [row[1][output_bit] for row in table]
    ones = sum(vals)
    n = len(table)
    input_names = [bit_to_port_name(ports, b) or f"net{b}" for b in input_bits]

    if len(input_bits) == 1:
        return ("BUFFER" if vals == [0, 1] else "INVERTER" if vals == [1, 0]
                else "CONST 1" if vals == [1, 1] else "CONST 0")
    if ones == 0:
        return "CONSTANT 0"
    if ones == n:
        return "CONSTANT 1"
    if all(v == (1 if all(c) else 0) for c, v in zip((r[0] for r in table), vals)):
        return f"AND of all inputs ({', '.join(input_names)})"
    if all(v == (1 if any(c) else 0) for c, v in zip((r[0] for r in table), vals)):
        return f"OR of all inputs ({', '.join(input_names)})"
    if all(v == (sum(c) % 2) for c, v in zip((r[0] for r in table), vals)):
        return f"XOR/parity of all inputs ({', '.join(input_names)})"
    if ones == 1:
        combo = next(c for c, v in zip((r[0] for r in table), vals) if v == 1)
        const = int("".join(map(str, combo)), 2)
        return f"EQUALITY: 1 iff raw input bits == {const} (0b{''.join(map(str, combo))})"
    if ones == n - 1:
        combo = next(c for c, v in zip((r[0] for r in table), vals) if v == 0)
        const = int("".join(map(str, combo)), 2)
        return f"INEQUALITY: 0 iff raw input bits == {const}"
    return None


def bits_to_int(bits_values):
    """bits_values: list of 0/1, index 0 = LSB (matches chain order)."""
    v = 0
    for i, b in enumerate(bits_values):
        v |= (b << i)
    return v


def classify_composed_arith(table, input_bits, output_bit, groups, group_of_bit):
    """Test: is output 1 exactly when (sum of the known register groups
    feeding this component) equals a single constant? Solves for the
    constant directly from the truth table instead of assuming it."""
    # Only meaningful if every input bit belongs to a KNOWN register group,
    # and there are at least 2 distinct groups involved.
    involved = set()
    for b in input_bits:
        g = group_of_bit.get(b)
        if g is None:
            return None
        involved.add(g)
    if len(involved) < 2:
        return None

    idx_of = {b: i for i, b in enumerate(input_bits)}
    sums_when_true, sums_when_false = set(), set()
    for combo, outs in table:
        group_vals = defaultdict(list)
        for gid in involved:
            bit_list = groups[gid]  # ordered LSB..MSB net ids
            for b in bit_list:
                if b in idx_of:
                    group_vals[gid].append(combo[idx_of[b]])
        total = sum(bits_to_int(group_vals[gid]) for gid in involved)
        (sums_when_true if outs[output_bit] else sums_when_false).add(total)

    if len(sums_when_true) == 1 and not (sums_when_true & sums_when_false):
        const = next(iter(sums_when_true))
        return f"EQUALITY on SUM of {len(involved)} register group(s): 1 iff (sum of groups) == {const}"
    return None


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <netlist.json> <top_module>", file=sys.stderr)
        sys.exit(1)
    json_path, top = sys.argv[1], sys.argv[2]
    cells, ports = load(json_path, top)
    net_driver, net_consumers = build_graphs(cells)

    print(f"=== Structural recovery report for '{top}' ===")
    print(f"Total gate-level cells: {len(cells)}\n")

    classified = set()

    # --- Registers / shift chains ---
    reg_chains, reg_singles = find_register_chains(cells, net_driver)
    reg_chains.sort(key=len, reverse=True)
    print(f"--- Register / shift-chain candidates ({len(reg_chains)} chain(s), "
          f"{len(reg_singles)} non-chained bit(s)) ---")
    group_of_bit = {}   # net (Q output bit) -> group index
    groups = {}          # group index -> ordered list of Q-output net ids (LSB..MSB)
    for i, members in enumerate(reg_chains):
        classified.update(members)
        q_bits = [cells[m]["connections"]["Q"][0] for m in members]
        groups[i] = q_bits
        for b in q_bits:
            group_of_bit[b] = i
        ctrl = group_control_signals(cells, ports, members)
        ctrl_str = "; ".join(f"{k}={'/'.join(sorted(v))}" for k, v in ctrl.items())
        print(f"  REG group {i}: {len(members)} bits wide  [{ctrl_str}]")
    if reg_singles:
        classified.update(reg_singles)
        print(f"  + {len(reg_singles)} standalone (non-chained) register bit(s)")
    print()

    # --- Everything else ---
    leftover_groups, leftover = leftover_components(cells, net_consumers, classified)
    leftover_groups.sort(key=len, reverse=True)
    print(f"--- Combinational logic ({len(leftover_groups)} connected component(s)) ---")
    for i, members in enumerate(leftover_groups):
        in_bits, out_bits = component_boundary(cells, members, net_driver, net_consumers)
        in_names = [bit_to_port_name(ports, b) or f"net{b}" for b in in_bits]
        out_names = [bit_to_port_name(ports, b) or f"net{b}" for b in out_bits]
        print(f"  component {i}: {len(members)} cells, {len(in_bits)} input bit(s), "
              f"{len(out_bits)} output bit(s)")
        print(f"    inputs:  {in_names}")
        print(f"    outputs: {out_names}")

        n_known_groups = len({group_of_bit[b] for b in in_bits if b in group_of_bit})
        if len(in_bits) > MAX_BRUTE_FORCE_INPUTS:
            print(f"    => too many inputs ({len(in_bits)}) to brute-force")
        else:
            table = simulate_component(cells, members, in_bits, out_bits)
            if table is None:
                print("    => simulation incomplete (unsupported cell type present)")
            else:
                for ob in out_bits:
                    ob_name = bit_to_port_name(ports, ob) or f"net{ob}"
                    label = classify_composed_arith(table, in_bits, ob, groups, group_of_bit)
                    if not label:
                        label = classify_single_bit(table, in_bits, ob, ports)
                    print(f"    [{ob_name}] => {label or 'UNCLASSIFIED'}")
        print()


if __name__ == "__main__":
    main()
