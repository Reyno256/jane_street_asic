#!/usr/bin/env python3
"""
Read a sum-of-products (OR-of-ANDs) structure directly off an AND/OR/NOT
gate network, instead of brute-forcing its truth table -- works when the
network genuinely has that shape (true here: the FSM's decode cloud is
483 AND / 414 OR / 240 NOT / only 41 XOR out of 1187 gates, so most local
neighborhoods are pure AND/OR/NOT trees). Flattens nested ORs into terms
and nested ANDs into literal lists; a literal whose own driver is neither
a raw net nor a simple AND/OR/NOT (e.g. an XOR or MUX, or itself another
OR -- meaning there's more structure worth expanding) is flagged rather
than silently treated as an opaque black box, so it's obvious where to
recurse (re-run this script with that net id as the new target).

Usage: extract_sop.py <netlist.json> <top_module> <target_net>
"""
import sys

sys.path.insert(0, "work")
from structural_recover import load, build_graphs, bit_to_port_name


def flatten_or(net, cells, net_driver, seen):
    drv = net_driver.get(net)
    if drv is not None and drv[0] not in seen:
        cname, _ = drv
        c = cells[cname]
        if c["type"] == "$_OR_":
            seen2 = seen | {cname}
            a, b = c["connections"]["A"][0], c["connections"]["B"][0]
            return flatten_or(a, cells, net_driver, seen2) + flatten_or(b, cells, net_driver, seen2)
    return [net]


def flatten_and(net, cells, net_driver, seen):
    """Returns a list of (polarity, net) literals for one AND-term."""
    drv = net_driver.get(net)
    if drv is not None and drv[0] not in seen:
        cname, _ = drv
        c = cells[cname]
        if c["type"] == "$_AND_":
            seen2 = seen | {cname}
            a, b = c["connections"]["A"][0], c["connections"]["B"][0]
            return flatten_and(a, cells, net_driver, seen2) + flatten_and(b, cells, net_driver, seen2)
        if c["type"] == "$_NOT_":
            a = c["connections"]["A"][0]
            return [(False, a)]
    return [(True, net)]


def describe_literal(net, polarity, cells, net_driver, ports):
    name = bit_to_port_name(ports, net) or f"net{net}"
    tag = name if polarity else f"~{name}"
    drv = net_driver.get(net)
    if drv is None:
        return tag  # true primary input/register output -- fully resolved
    ctype = cells[drv[0]]["type"]
    if ctype in ("$_OR_", "$_XOR_", "$_MUX_", "$_AND_", "$_NOT_"):
        # AND/NOT would already have been flattened away by flatten_and,
        # so seeing one here means it belongs to a *different* seen-set
        # branch (shared subexpression) -- still worth flagging to recurse.
        return f"{tag}  [driven by {ctype} -- re-run with target_net={net} to expand]"
    return tag


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <netlist.json> <top_module> <target_net>", file=sys.stderr)
        sys.exit(1)
    json_path, top, target = sys.argv[1:4]
    target = int(target)
    cells, ports = load(json_path, top)
    net_driver, net_consumers = build_graphs(cells)

    or_terms = flatten_or(target, cells, net_driver, set())
    label = bit_to_port_name(ports, target) or f"net{target}"
    print(f"{label} = OR of {len(or_terms)} term(s):\n")
    for i, term_root in enumerate(or_terms):
        literals = flatten_and(term_root, cells, net_driver, set())
        parts = [describe_literal(n, pol, cells, net_driver, ports) for pol, n in literals]
        print(f"  term {i} ({len(literals)} literals): " + " & ".join(parts))


if __name__ == "__main__":
    main()
