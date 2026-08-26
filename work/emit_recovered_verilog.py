#!/usr/bin/env python3
"""
Emit a hierarchical Verilog file reflecting the structure found by
structural_recover.py: each discovered register chain becomes an instance
of a small, verified shift-register module, and each leftover
combinational component becomes its own named module containing the
*actual* recovered gates (mechanically regenerated from the JSON netlist,
not abstracted away).

Adder/comparator splitting: when a combinational component is driven by
2+ known register groups, its full internal net list is searched for
literal wires that equal a bit of (groupA + groupB + ...) for every input
combination (find_literal_sum_bit_nets) -- these are the REAL sum bits,
just never exposed as a component "boundary" net because nothing outside
the fused block consumes them directly. Wherever any such bits are found,
the backward logic cone feeding them (backward_cone) is carved out into
its own ADDER module, and the rest of the component becomes a separate
COMPARATOR module that consumes the adder's outputs -- this is a genuine
structural split of the real gates (not a behavioral reconstruction),
recovering the adder as its own visible block instead of one fused blob.

Usage: emit_recovered_verilog.py <netlist.json> <top_module_name> <out.v>
"""
import sys

sys.path.insert(0, "work")
from structural_recover import (
    COMB_GATES, UNARY_GATES, MAX_BRUTE_FORCE_INPUTS,
    backward_cone, build_graphs, bit_to_port_name, classify_composed_arith,
    classify_single_bit, component_boundary, find_literal_sum_bit_nets,
    find_register_chains, leftover_components, load,
    simulate_component,
)

GATE_VERILOG_OP = {
    "$_AND_": "and", "$_OR_": "or", "$_XOR_": "xor",
    "$_NAND_": "nand", "$_NOR_": "nor", "$_XNOR_": "xnor",
    "$_NOT_": "not", "$_BUF_": "buf",
}


def net_name(bit, ports):
    p = bit_to_port_name(ports, bit)
    if p:
        return p.replace("[", "_").replace("]", "")
    return f"w{bit}"


def emit_comb_module(mod_name, cells, members, in_bits, out_bits, ports):
    lines = [f"module {mod_name} ("]
    port_decls = [f"    input {net_name(b, ports)}" for b in in_bits]
    port_decls += [f"    output {net_name(b, ports)}" for b in out_bits]
    lines.append(",\n".join(port_decls))
    lines.append(");")

    boundary = set(in_bits) | set(out_bits)
    internal_nets = set()
    for m in members:
        c = cells[m]
        for pname, bits in c["connections"].items():
            for b in bits:
                if isinstance(b, str) or b in boundary:
                    continue
                internal_nets.add(b)
    for b in sorted(internal_nets):
        lines.append(f"    wire {net_name(b, ports)};")

    ASSIGN_OP = {"$_AND_": "&", "$_OR_": "|", "$_XOR_": "^",
                 "$_NAND_": "~&", "$_NOR_": "~|", "$_XNOR_": "~^"}
    for m in members:
        c = cells[m]
        ctype = c["type"]
        conns = c["connections"]
        if ctype in UNARY_GATES:
            y = net_name(conns["Y"][0], ports)
            a = net_name(conns["A"][0], ports)
            op = "~" if ctype == "$_NOT_" else ""
            lines.append(f"    assign {y} = {op}{a};")
        elif ctype in COMB_GATES:
            y = net_name(conns["Y"][0], ports)
            a = net_name(conns["A"][0], ports)
            b = net_name(conns["B"][0], ports)
            op = ASSIGN_OP[ctype]
            if op.startswith("~"):
                lines.append(f"    assign {y} = ~({a} {op[1]} {b});")
            else:
                lines.append(f"    assign {y} = {a} {op} {b};")
        elif ctype == "$_MUX_":
            # Verilog has no built-in mux gate primitive (Y = S ? B : A,
            # verified against Yosys's own simcells.v) -- use a continuous
            # assign instead.
            y = net_name(conns["Y"][0], ports)
            a = net_name(conns["A"][0], ports)
            b = net_name(conns["B"][0], ports)
            s = net_name(conns["S"][0], ports)
            lines.append(f"    assign {y} = {s} ? {b} : {a};")
        else:
            raise ValueError(f"unsupported cell type in comb block: {ctype}")
    lines.append("endmodule\n")
    return "\n".join(lines)


def shift_reg_module_name(width):
    return f"SHIFT_REG_EN_{width}"


def shift_reg_module(width):
    # Verified shift register: chain traversal confirmed D(stage i) is
    # driven directly by Q(stage i-1), with the first stage fed from
    # outside the chain -- this is the exact, confirmed behavior, not an
    # assumption. Parameterized by width (an earlier version hardcoded 8
    # bits, copied from the warmup's genuinely-8-bit chain, without
    # generalizing -- silently produced out-of-range bit selects for any
    # other chain length, e.g. the puzzle's 12-bit chain, which Verilog
    # resolves to 'x' rather than an error).
    name = shift_reg_module_name(width)
    return f"""\
module {name} (clk, rst_n, en, serial_in, q);
    input clk, rst_n, en, serial_in;
    output [{width-1}:0] q;
    reg [{width-1}:0] q;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) q <= {width}'b0;
        else if (en) q <= {{q[{width-2}:0], serial_in}};
endmodule
"""

# One reusable 1-bit register primitive per DFF type actually seen in the
# design. These are NOT chained (see SHIFT_REG_EN for that) -- each
# standalone (non-chained) register in the netlist gets its own instance
# of the matching module below, wired to its real D/Q/C/R/E connections.
# Semantics for each verified from the real PDK source earlier in this
# project (see project memory), not guessed:
#   $_DFF_PN0_   -- posedge clock, async active-low RESET to 0
#   $_DFF_PN1_   -- posedge clock, async active-low SET to 1 (sky130 dfstp)
#   $_DFF_P_     -- posedge clock, no reset/set at all (sky130 dfxtp)
#   $_DFFE_PN0P_ -- posedge clock, async active-low RESET to 0, active-high enable
REG_PRIMITIVE_DEFS = {
    "$_DFF_PN0_": ("DFF_PN0", """\
module DFF_PN0 (C, D, R, Q);
    input C, D, R;
    output reg Q;
    always @(posedge C or negedge R)
        if (!R) Q <= 1'b0;
        else    Q <= D;
endmodule
"""),
    "$_DFF_PN1_": ("DFF_PN1", """\
module DFF_PN1 (C, D, R, Q);
    input C, D, R;
    output reg Q;
    always @(posedge C or negedge R)
        if (!R) Q <= 1'b1;
        else    Q <= D;
endmodule
"""),
    "$_DFF_P_": ("DFF_P", """\
module DFF_P (C, D, Q);
    input C, D;
    output reg Q;
    always @(posedge C) Q <= D;
endmodule
"""),
    "$_DFFE_PN0P_": ("DFFE_PN0P", """\
module DFFE_PN0P (C, D, E, R, Q);
    input C, D, E, R;
    output reg Q;
    always @(posedge C or negedge R)
        if (!R)      Q <= 1'b0;
        else if (E)  Q <= D;
endmodule
"""),
}


def control_net(cells, members, port_letter):
    """Raw net ID (not a pre-formatted name -- see the module docstring's
    note on why group_control_signals()'s own naming caused a real bug)
    for a shared control port (C/R/E) across a register chain's members."""
    nets = {cells[m]["connections"][port_letter][0] for m in members
            if port_letter in cells[m]["connections"]}
    if not nets:
        return None
    if len(nets) > 1:
        raise ValueError(f"chain members disagree on {port_letter} net: {nets}")
    return next(iter(nets))


def try_split_adder(cells, members, in_bits, out_bits, net_driver, net_consumers,
                     groups, group_of_bit):
    """Returns None if no split was found, else a dict describing the two
    new sub-components (adder_cells/adder_bounds, cmp_cells/cmp_bounds,
    found_bits)."""
    involved = sorted({group_of_bit[b] for b in in_bits if b in group_of_bit})
    if len(involved) < 2:
        return None
    found = find_literal_sum_bit_nets(cells, members, in_bits, involved, groups, group_of_bit)
    if not found:
        return None

    target_nets = list(found.values())
    adder_cells = backward_cone(cells, target_nets, members, net_driver)
    cmp_cells = [m for m in members if m not in adder_cells]
    if not adder_cells or not cmp_cells:
        return None

    adder_in, adder_out = component_boundary(cells, adder_cells, net_driver, net_consumers)
    cmp_in, cmp_out = component_boundary(cells, cmp_cells, net_driver, net_consumers)
    return {
        "found_bits": found,
        "adder_cells": sorted(adder_cells), "adder_in": adder_in, "adder_out": adder_out,
        "cmp_cells": cmp_cells, "cmp_in": cmp_in, "cmp_out": cmp_out,
    }


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <netlist.json> <top_module> <out.v>", file=sys.stderr)
        sys.exit(1)
    json_path, top, out_path = sys.argv[1:4]
    cells, ports = load(json_path, top)
    net_driver, net_consumers = build_graphs(cells)

    reg_chains, reg_singles = find_register_chains(cells, net_driver)
    reg_chains.sort(key=len, reverse=True)

    classified = set()
    groups = {}
    group_of_bit = {}
    reg_info = []
    for i, members in enumerate(reg_chains):
        classified.update(members)
        q_bits = [cells[m]["connections"]["Q"][0] for m in members]
        groups[i] = q_bits
        for b in q_bits:
            group_of_bit[b] = i
        ctrl_nets = {p: control_net(cells, members, p) for p in ("C", "R", "E")}
        first = members[0]
        serial_in_net = cells[first]["connections"]["D"][0]
        reg_info.append((i, members, ctrl_nets, serial_in_net, q_bits))
    classified.update(reg_singles)

    # Standalone (non-chained) registers are real design state too -- an
    # earlier version of this script excluded them from combinational
    # clustering (correct) but then never actually instantiated them in
    # the output at all (a real bug: they'd silently become undriven
    # floating wires, syntactically legal in Verilog so `iverilog` never
    # complained, but semantically missing -- caught by noticing only 1
    # of many known register groups showed up in a design with dozens of
    # standalone FSM state bits).
    used_reg_types = {cells[m]["type"] for m in reg_singles}
    unknown_types = used_reg_types - set(REG_PRIMITIVE_DEFS)
    if unknown_types:
        raise ValueError(f"no primitive module defined for register type(s): {unknown_types} "
                          f"-- add them to REG_PRIMITIVE_DEFS")

    leftover_groups, leftover = leftover_components(cells, net_consumers, classified)
    leftover_groups.sort(key=len, reverse=True)

    out = []
    out.append("// Auto-generated by emit_recovered_verilog.py from structural_recover.py's findings.")
    out.append("// Register groups are verified shift registers (see SHIFT_REG_EN). Combinational")
    out.append("// blocks contain the ACTUAL recovered gates, mechanically regenerated from the")
    out.append("// gate-level netlist -- nothing here is abstracted away or assumed. Where an adder")
    out.append("// was recoverable as real internal wires (see find_literal_sum_bit_nets), it is")
    out.append("// shown as its own ADDER block rather than fused into one comparator blob.\n")
    chain_widths = sorted({len(q_bits) for _, _, _, _, q_bits in reg_info})
    for width in chain_widths:
        out.append(shift_reg_module(width))
    for t in sorted(used_reg_types):
        out.append(REG_PRIMITIVE_DEFS[t][1])

    top_insts = []  # (mod_name, instname, conns_list) built up as we go

    def input_source(b):
        gid = group_of_bit.get(b)
        if gid is not None:
            qwire, q_bits = reg_q_wire[gid]
            return f"{qwire}[{q_bits.index(b)}]"
        return bit_to_port_name(ports, b) or net_name(b, ports)

    reg_q_wire = {}
    for i, members, ctrl_nets, serial_in_net, q_bits in reg_info:
        qwire = f"reg{i}_q"
        reg_q_wire[i] = (qwire, q_bits)

    for j, members in enumerate(leftover_groups):
        in_bits, out_bits = component_boundary(cells, members, net_driver, net_consumers)
        overall_label = None
        if len(in_bits) <= MAX_BRUTE_FORCE_INPUTS:
            table = simulate_component(cells, members, in_bits, out_bits)
            if table is not None:
                labels = []
                for ob in out_bits:
                    l = classify_composed_arith(table, in_bits, ob, groups, group_of_bit)
                    if not l:
                        l = classify_single_bit(table, in_bits, ob, ports)
                    labels.append(l)
                overall_label = "; ".join(l for l in labels if l)

        split = try_split_adder(cells, members, in_bits, out_bits, net_driver,
                                 net_consumers, groups, group_of_bit)

        if split:
            found = split["found_bits"]
            bits_found = sorted(found)
            expected_width = max(len(groups[g]) for g in
                                  {group_of_bit[b] for b in in_bits if b in group_of_bit}) + 1
            missing = [b for b in range(expected_width) if b not in found]
            adder_name = f"ADDER_{j}"
            cmp_name = f"COMPARATOR_{j}"

            out.append(f"// {adder_name}: recovered as real gates -- literal internal wires were "
                       f"found matching sum bit(s) {bits_found} of the input register groups"
                       + (f" (bit(s) {missing} not present as a standalone wire -- fused into "
                          f"{cmp_name}'s logic instead)" if missing else "") + ".")
            out.append(emit_comb_module(adder_name, cells, split["adder_cells"],
                                         split["adder_in"], split["adder_out"], ports))
            out.append(f"// {cmp_name}: recovered as real gates -- consumes {adder_name}'s outputs"
                       f"{' plus residual signal(s) directly' if len(split['cmp_in']) > len(bits_found) else ''}."
                       f" Together with {adder_name}, verified overall behavior: {overall_label}.")
            out.append(emit_comb_module(cmp_name, cells, split["cmp_cells"],
                                         split["cmp_in"], split["cmp_out"], ports))

            top_insts.append((adder_name, f"add_{j}", split["adder_in"], split["adder_out"]))
            top_insts.append((cmp_name, f"cmp_{j}", split["cmp_in"], split["cmp_out"]))
        else:
            mod_name = f"COMB_BLOCK_{j}"
            if overall_label and "EQUALITY on SUM" in overall_label:
                mod_name = f"EQ_CHECK_{j}"
            if overall_label:
                out.append(f"// {mod_name}: verified functional behavior -- {overall_label}")
            out.append(emit_comb_module(mod_name, cells, members, in_bits, out_bits, ports))
            top_insts.append((mod_name, f"comb_{j}", in_bits, out_bits))

    # --- Top module ---
    top_ports = list(ports.keys())
    out.append(f"module {top}_recovered (")
    out.append(",\n".join(f"    {ports[p]['direction']} {p}" for p in top_ports))
    out.append(");\n")

    for i, members, ctrl_nets, serial_in_net, q_bits in reg_info:
        # Resolved via input_source (consistent with net_name() everywhere
        # else), NOT group_control_signals()'s own independent naming --
        # that mismatch (e.g. "net177" vs the actually-driven "w177") was
        # a real bug: any chain control signal that isn't itself a raw
        # top-level port (e.g. a gated/derived enable, as here) silently
        # became a disconnected, undriven wire with a different name.
        clk = input_source(ctrl_nets["C"])
        rst = input_source(ctrl_nets["R"])
        en = input_source(ctrl_nets["E"])
        serial_in_name = bit_to_port_name(ports, serial_in_net) or net_name(serial_in_net, ports)
        qwire, qb = reg_q_wire[i]
        width = len(qb)
        out.append(f"    wire [{width-1}:0] {qwire};")
        out.append(f"    {shift_reg_module_name(width)} reg_{i} (")
        out.append(f"        .clk({clk}), .rst_n({rst}), .en({en}),")
        out.append(f"        .serial_in({serial_in_name}), .q({qwire})")
        out.append(f"    );\n")

    for m in reg_singles:
        c = cells[m]
        mod_name, _ = REG_PRIMITIVE_DEFS[c["type"]]
        conns = c["connections"]
        q_net = conns["Q"][0]
        q_name = input_source(q_net)  # port name if Q is itself a top-level output (e.g. `success`)
        port_conns = []
        for p in ("C", "D", "E", "R"):
            if p in conns:
                port_conns.append(f".{p}({input_source(conns[p][0])})")
        port_conns.append(f".Q({q_name})")
        inst = f"reg_{m.replace('$', '_').replace(':', '_').replace('.', '_')}"
        out.append(f"    {mod_name} {inst} (")
        out.append(",\n".join(f"        {pc}" for pc in port_conns))
        out.append(f"    );\n")

    for mod_name, instname, in_bits, out_bits in top_insts:
        conns = []
        for b in in_bits:
            conns.append(f".{net_name(b, ports)}({input_source(b)})")
        for b in out_bits:
            dst = bit_to_port_name(ports, b) or net_name(b, ports)
            conns.append(f".{net_name(b, ports)}({dst})")
        out.append(f"    {mod_name} {instname} (")
        out.append(",\n".join(f"        {c}" for c in conns))
        out.append(f"    );\n")

    # Declare any intermediate wires used purely between two sub-modules
    # (adder output feeding the comparator, or a standalone register's
    # output/inputs) that aren't top-level ports.
    declared = {p for p in ports}
    needed_wires = set()
    for mod_name, instname, in_bits, out_bits in top_insts:
        for b in in_bits + out_bits:
            name = net_name(b, ports)
            if bit_to_port_name(ports, b) is None:
                needed_wires.add(name)
    for m in reg_singles:
        for p in ("C", "D", "E", "R", "Q"):
            bits = cells[m]["connections"].get(p)
            if not bits:
                continue
            b = bits[0]
            if bit_to_port_name(ports, b) is None:
                needed_wires.add(net_name(b, ports))
    wire_decls = [f"    wire {w};" for w in sorted(needed_wires)]
    if wire_decls:
        # insert right after the port list's closing ");" + blank line
        insert_at = next(i for i, l in enumerate(out) if l == f"module {top}_recovered (")
        # find the ");\n" line index following it
        close_idx = next(i for i in range(insert_at, len(out)) if out[i] == ");\n")
        out[close_idx + 1:close_idx + 1] = wire_decls + [""]

    out.append("endmodule")

    with open(out_path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {out_path}: {len(reg_info)} register group(s), {len(top_insts)} logic block instance(s)")
    for mod_name, instname, in_bits, out_bits in top_insts:
        print(f"  {mod_name}")


if __name__ == "__main__":
    main()
