#!/usr/bin/env python3
"""
Mechanically transcribe sky130_fd_sc_hd combinational-cell behavioral
Verilog from the real PDK source (sky130_fd_sc_hd.v), instead of
hand-deriving each equation by eye (as was done for the warmup's 16 cell
types -- correct, but doesn't scale to the ~50 additional types the real
puzzle design uses).

Method, applied uniformly and mechanically (not "guessed" per cell):
For each requested cell, find its FUNCTIONAL module body (the `else` branch
of `\ifdef FUNCTIONAL`, i.e. the one NOT needing USE_POWER_PINS-independent
form -- we always want the USE_POWER_PINS variant since our LEF-based pin
list always includes VPWR/VGND/VPB/VNB) and extract:
  - the declared port list + directions ("Module ports" section)
  - every gate-primitive instance line (and/or/nand/nor/not/xor/xnor/buf --
    Verilog's *built-in* primitives, not sky130's UDPs)
The sky130_fd_sc_hd__udp_pwrgood_pp$PG wrapper (present on almost every
cell's real output) is a power-rail-validity check that just passes its
data input through under ideal power -- it is dropped, and the true output
wire is aliased directly to whatever fed the pwrgood cell's data input.

Any cell whose body still contains a *different* UDP after removing
pwrgood (only expected for flip-flops, which are handled separately by
hand -- see sky130_fd_sc_hd_behavioral_min.v) is reported and skipped
rather than silently mistranslated.

Usage: gen_cell_behaviors.py <cell_name> [<cell_name> ...]
Prints the generated Verilog to stdout.
"""
import re
import sys

PDK_V = ("/Users/quinn/.volare/volare/sky130/versions/"
         "7519dfb04400f224f140749cda44ee7de6f5e095/sky130A/libs.ref/"
         "sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v")

GATE_KEYWORDS = {"and", "or", "nand", "nor", "not", "xor", "xnor", "buf"}


def find_use_power_pins_module(text, cellname):
    """Return the module body text for cellname's USE_POWER_PINS branch.

    Finds every exact `module <cellname> (` declaration (word-boundaried,
    so e.g. "and2" doesn't match inside "and2b"), then for each picks the
    nearest preceding `ifdef/else` marker to determine which branch it's
    in, keeping only the USE_POWER_PINS one. Each module's own end is the
    FIRST "endmodule" after its start -- correct as long as the start is
    exactly right, which the anchoring above guarantees."""
    decl_re = re.compile(rf"module {re.escape(cellname)} \(")
    branch_re = re.compile(r"`ifdef USE_POWER_PINS|`else // If not USE_POWER_PINS")
    for m in decl_re.finditer(text):
        # nearest branch marker strictly before this declaration
        branch_marker = None
        for bm in branch_re.finditer(text, 0, m.start()):
            branch_marker = bm
        if branch_marker is None or "ifdef USE_POWER_PINS" not in branch_marker.group(0):
            continue
        end = text.find("endmodule", m.start())
        if end == -1:
            continue
        return text[m.start():end + len("endmodule")]
    return None


def parse_ports(body):
    """Port declarations (`input X;` / `output X;`) only ever appear in the
    ports block of these files -- not requiring a preceding comment marker
    (some thin wrapper modules, e.g. a strength-specific cell that just
    instantiates its shared base cell, omit the "// Module ports" comment
    that full definitions have)."""
    ports = []
    for line in body.splitlines():
        s = line.strip()
        m = re.match(r"(input|output)\s+([A-Za-z0-9_]+)\s*;", s)
        if m:
            ports.append((m.group(1), m.group(2)))
    return ports


def parse_gate_instances(body):
    """Returns (instances, other_udp_refs). instances: list of
    (keyword, output_signal, [input_signals]). Drops pwrgood wrapper
    instances, returning a rewrite so their output aliases their input."""
    instances = []
    pwrgood_alias = {}  # pwrgood output signal -> its data input signal
    other_udp = []
    # Lines that are legitimately not gate instances -- anything else that
    # doesn't match the instance pattern below is treated as an error
    # (added to other_udp) rather than silently skipped, so a line this
    # parser doesn't understand can never disappear without a trace (this
    # is exactly how dfxtp/dfstp's DFF instantiation line -- which has an
    # embedded `UNIT_DELAY macro token the original instance regex didn't
    # expect -- silently vanished, leaving a broken flip-flop body with no
    # error raised).
    ignorable_re = re.compile(
        r"^(module\b.*|output\b.*;|input\b.*;|wire\b.*;|//.*|"
        r"end(module)?|\)\s*;?|[\w\s,;]*|)$"
    )
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        # Tolerate an optional backtick-prefixed delay macro (e.g.
        # `UNIT_DELAY) between the cell type and instance name.
        m = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)\s+(?:`\w+\s+)?\S+\s*\(([^)]*)\)\s*;", s)
        if not m:
            if ignorable_re.match(s):
                continue
            other_udp.append(f"UNPARSED LINE: {s!r}")
            continue
        keyword, argstr = m.group(1), m.group(2)
        args = [a.strip() for a in argstr.split(",")]
        if "pwrgood" in keyword:
            # sky130_fd_sc_hd__udp_pwrgood_pp$PG (out, data_in, VPWR, VGND)
            pwrgood_alias[args[0]] = args[1]
            continue
        if keyword not in GATE_KEYWORDS:
            other_udp.append(s)
            continue
        out_sig, in_sigs = args[0], args[1:]
        instances.append([keyword, out_sig, in_sigs])
    # Resolve pwrgood aliasing: anywhere a gate's inputs reference a
    # pwrgood output, substitute the real upstream signal instead, and
    # rename the true module output net accordingly.
    for inst in instances:
        inst[2] = [pwrgood_alias.get(s, s) for s in inst[2]]
    return instances, pwrgood_alias, other_udp


def gen_module(target_name, text):
    """target_name may be a drive-strength-suffixed name (e.g.
    sky130_fd_sc_hd__clkbuf_4) that isn't itself a distinct logical
    definition in the PDK source -- those are thin wrappers around a
    shared base cell (e.g. sky130_fd_sc_hd__clkbuf) differing only in
    transistor sizing, which doesn't affect logical behavior. If the exact
    name isn't found, strip a trailing _<digits> and retry against the
    base cell, emitting the result under the originally requested name."""
    body = find_use_power_pins_module(text, target_name)
    cellname = target_name
    # A thin wrapper's body just instantiates the shared base cell by name
    # (`sky130_fd_sc_hd__<base> base (.X(X), ...)`, named connections, no
    # inline gate primitives) -- not usable directly, fall through to the
    # base cell's own (fully-defined) body instead.
    if body is not None and re.search(r"sky130_fd_sc_hd__\w+\s+base\s*\(", body):
        body = None
    if body is None:
        base = re.sub(r"_\d+$", "", target_name)
        if base != target_name:
            body = find_use_power_pins_module(text, base)
            cellname = base
    if body is None:
        return None, f"module body not found for {target_name}"
    ports = parse_ports(body)
    if not ports:
        return None, f"no ports parsed for {cellname}"
    instances, pwrgood_alias, other_udp = parse_gate_instances(body)
    if other_udp:
        return None, f"{cellname} uses non-gate primitive(s), needs manual review: {other_udp}"

    port_decl = "; ".join(f"{d} {n}" for d, n in ports if n not in ("VPWR", "VGND", "VPB", "VNB"))
    power_decl = "input VPWR, VGND, VPB, VNB"
    port_names = ", ".join(n for _, n in ports)

    lines = [f"module {target_name} ({port_names});", f"    {port_decl}; {power_decl};"]

    # Any pwrgood output that is ALSO a genuine module output port must be
    # wired via assign (its "real" driver got renamed away during aliasing).
    port_output_names = {n for d, n in ports if d == "output"}
    for pg_out, real_driver in pwrgood_alias.items():
        if pg_out in port_output_names:
            lines.append(f"    assign {pg_out} = {real_driver};")

    for keyword, out_sig, in_sigs in instances:
        args = ", ".join([out_sig] + in_sigs)
        lines.append(f"    {keyword} ({args});")
    lines.append("endmodule\n")
    return "\n".join(lines), None


def main():
    with open(PDK_V) as f:
        text = f.read()
    ok, failed = [], []
    for cellname in sys.argv[1:]:
        mod, err = gen_module(cellname, text)
        if mod:
            ok.append(mod)
        else:
            failed.append((cellname, err))
    print("\n".join(ok))
    if failed:
        print("\n// ==== FAILED TO AUTO-GENERATE (needs manual review) ====", file=sys.stderr)
        for cellname, err in failed:
            print(f"// {cellname}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
