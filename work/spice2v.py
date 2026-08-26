#!/usr/bin/env python3
"""
Minimal, dependency-free SPICE (ngspice, as written by Magic's ext2spice)
to structural Verilog converter.

Written as a workaround for a Netgen 1.5.323 segfault (SIGSEGV) in its
`writenet verilog` C writer when converting a netlist made almost entirely
of black-box (zero-internal-device) subcircuits -- exactly what you get
from a LEF-blackboxed standard-cell extraction. Netgen remains fine to use
for LVS (readnet/lvs), which doesn't go through the crashing code path;
this script only replaces the Verilog *writing* step.

SPICE subcircuit pins are positional and carry no direction info, so pin
directions for each standard cell are pulled from the LEF (which declares
DIRECTION INPUT/OUTPUT/INOUT per pin -- ground truth, since ext2spice wrote
each blackboxed subckt's pin list in that same LEF pin order). Power/ground
pins are LEF-INOUT but are declared `input` here since some Verilog tooling
(e.g. some online netlist viewers) rejects `inout` outright and a supply
rail behaves like a constant input in practice. Top-level module port
direction is inferred: a port is `output` if any internal cell instance
drives it from an OUTPUT pin, otherwise `input`.

Usage: spice2v.py <in.spice> <lef.lef> <top_subckt_name> <out.v>
"""
import re
import sys


def parse_spice(path):
    with open(path) as f:
        lines = f.readlines()

    # Strip comments (lines starting with '*') and join '+' continuations.
    joined = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("*"):
            continue
        if line.startswith("+"):
            joined[-1] += " " + line[1:].strip()
        else:
            joined.append(line.strip())

    subckts = {}  # name -> {"pins": [...], "insts": [(instname, nets, celltype), ...]}
    cur = None
    for line in joined:
        if line.lower().startswith(".subckt"):
            toks = line.split()
            name = toks[1]
            pins = toks[2:]
            cur = {"pins": pins, "insts": []}
            subckts[name] = cur
        elif line.lower().startswith(".ends"):
            cur = None
        elif line.startswith("X") or line.startswith("x"):
            toks = line.split()
            instname = toks[0][1:]
            celltype = toks[-1]
            nets = toks[1:-1]
            if cur is not None:
                cur["insts"].append((instname, nets, celltype))
    return subckts


def parse_lef_pin_directions(path):
    """Returns {macro_name: {pin_name: 'INPUT'|'OUTPUT'|'INOUT'}}."""
    with open(path) as f:
        text = f.read()

    dirs = {}
    cur_macro = None
    cur_pin = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^MACRO\s+(\S+)", s)
        if m:
            cur_macro = m.group(1)
            dirs[cur_macro] = {}
            continue
        if s.startswith("END") and cur_macro and s == f"END {cur_macro}":
            cur_macro = None
            continue
        m = re.match(r"^PIN\s+(\S+)", s)
        if m and cur_macro:
            cur_pin = m.group(1)
            continue
        m = re.match(r"^DIRECTION\s+(\S+)\s*;", s)
        if m and cur_macro and cur_pin:
            dirs[cur_macro][cur_pin] = m.group(1)
            cur_pin = None
    return dirs


def v_ident(name):
    """Make a SPICE token into a legal raw Verilog identifier."""
    name = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    if re.match(r"^[0-9]", name):
        name = "_" + name
    return name


def pin_verilog_dir(lef_dirs, celltype, pin):
    d = lef_dirs.get(celltype, {}).get(pin, "INPUT")
    if d == "OUTPUT":
        return "output"
    if d == "INOUT":
        # Power/ground rails (the only INOUT pins standard cells have) --
        # treat as input; some Verilog tooling rejects `inout` outright.
        return "input"
    return "input"


def is_power_only(lef_dirs, celltype):
    """True if every pin on this cell type is LEF-INOUT (i.e. it's a pure
    power/ground filler cell like a decap or substrate tap -- no signal
    pins at all, so it carries zero logical function)."""
    pins = lef_dirs.get(celltype)
    if not pins:
        return False
    return all(d == "INOUT" for d in pins.values())


def infer_top_port_dirs(sub, subckts, lef_dirs):
    """A top-level port is `output` if some internal instance drives it
    from an OUTPUT pin; otherwise `input`."""
    driven = set()
    for _, nets, celltype in sub["insts"]:
        child = subckts.get(celltype)
        if child is None:
            continue
        for pin, net in zip(child["pins"], nets):
            if pin_verilog_dir(lef_dirs, celltype, pin) == "output":
                driven.add(net)
    return {p: ("output" if p in driven else "input") for p in sub["pins"]}


def write_verilog(subckts, lef_dirs, top, out_path, emit_stubs=True, drop_filler=False):
    lines = []
    lines.append("// Structural Verilog generated from SPICE via spice2v.py")
    lines.append("// Leaf-cell pin directions come from the sky130_fd_sc_hd LEF.")
    lines.append("// Top-level port direction is inferred from internal driver pins.")
    if not emit_stubs:
        lines.append("// NOTE: leaf cells are NOT defined here (--no-stubs) -- pair this")
        lines.append("// file with the PDK's real sky130_fd_sc_hd.v behavioral models,")
        lines.append("// e.g. for simulators/viewers (like digitaljs) that need actual")
        lines.append("// gate/flop behavior, not just an empty black-box shell.")
    if drop_filler:
        lines.append("// NOTE: power-only filler instances (decap/tap cells -- all pins")
        lines.append("// INOUT, no signal pins, zero logic function) were dropped (--drop-filler).")
    lines.append("")

    if top not in subckts:
        raise SystemExit(f"top subckt '{top}' not found in netlist")

    top_port_dirs = infer_top_port_dirs(subckts[top], subckts, lef_dirs)

    def emit_module(name, sub, define_body, port_dirs):
        pins = sub["pins"]
        lines.append(f"module {v_ident(name)} (")
        lines.append(",\n".join(f"    {port_dirs[p]} {v_ident(p)}" for p in pins))
        lines.append(");")
        if define_body:
            for instname, nets, celltype in sub["insts"]:
                if drop_filler and is_power_only(lef_dirs, celltype):
                    continue
                child = subckts.get(celltype)
                if child is None:
                    # Unknown cell type (e.g. a router-generated VIA_* primitive
                    # with no subckt header captured) -- skip silently, nothing
                    # to instantiate against.
                    continue
                child_pins = child["pins"]
                conns = ",\n".join(
                    f"        .{v_ident(pin)}({v_ident(net)})"
                    for pin, net in zip(child_pins, nets)
                )
                lines.append(f"    {v_ident(celltype)} {v_ident(instname)} (")
                lines.append(conns)
                lines.append("    );")
        lines.append("endmodule")
        lines.append("")

    # Stub (empty) modules for every leaf cell type the top module
    # references, so the file parses standalone. Real behavioral models
    # (e.g. the PDK's sky130_fd_sc_hd.v) should be used instead for
    # simulation -- these stubs are for syntax/structure viewing only.
    if emit_stubs:
        referenced = {celltype for _, _, celltype in subckts[top]["insts"]}
        for name in sorted(referenced):
            if drop_filler and is_power_only(lef_dirs, name):
                continue
            if name in subckts and name != top:
                leaf_dirs = {p: pin_verilog_dir(lef_dirs, name, p) for p in subckts[name]["pins"]}
                emit_module(name, subckts[name], define_body=False, port_dirs=leaf_dirs)

    emit_module(top, subckts[top], define_body=True, port_dirs=top_port_dirs)

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    args = sys.argv[1:]
    emit_stubs = True
    drop_filler = False
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--no-stubs":
            emit_stubs = False
        elif flag == "--drop-filler":
            drop_filler = True
        else:
            print(f"unknown flag: {flag}", file=sys.stderr)
            sys.exit(1)

    if len(args) != 4:
        print(f"usage: {sys.argv[0]} [--no-stubs] [--drop-filler] "
              f"<in.spice> <lef.lef> <top_subckt_name> <out.v>", file=sys.stderr)
        sys.exit(1)
    spice_path, lef_path, top, out_path = args
    subckts = parse_spice(spice_path)
    lef_dirs = parse_lef_pin_directions(lef_path)
    write_verilog(subckts, lef_dirs, top, out_path, emit_stubs=emit_stubs, drop_filler=drop_filler)
    kept = subckts[top]["insts"]
    if drop_filler:
        kept = [i for i in kept if not is_power_only(lef_dirs, i[2])]
    print(f"wrote {out_path}: top={top} {len(kept)} insts kept "
          f"(of {len(subckts[top]['insts'])} total), "
          f"{len({c for _, _, c in kept})} unique cell types")


if __name__ == "__main__":
    main()
