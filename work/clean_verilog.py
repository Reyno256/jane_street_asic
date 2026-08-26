#!/usr/bin/env python3
"""
Cleanup pass for Verilog netlists written by Netgen's `writenet verilog`.

Netgen's Verilog writer doesn't escape identifiers, so it can emit two kinds
of invalid raw Verilog:

1. '/' inside identifiers -- happens for nets/instances Netgen had to
   auto-name (e.g. unconnected body-tie pins like VPB/VNB, which are tied
   through the substrate rather than a labeled metal net, so Magic's
   extractor never gives them a real net name). Netgen falls back to
   "<instance>/<pin>"-style names. We rewrite '/' to '_' wherever it sits
   next to an identifier character, while leaving '/*' and '*/' comment
   delimiters alone.

2. Trailing commas before a closing ')' in port declaration / instance
   connection lists -- not legal in standard Verilog port-list syntax.
"""
import re
import sys


def clean(text: str) -> str:
    # Replace '/' with '_' only when adjacent to an identifier character,
    # so comment markers /* and */ are untouched.
    text = re.sub(r"(?<=[A-Za-z0-9_])/|/(?=[A-Za-z0-9_])", "_", text)
    # Drop a trailing comma that's immediately followed by a closing ');'.
    text = re.sub(r",(\s*\)\s*;)", r"\1", text)
    return text


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <in.v> <out.v>", file=sys.stderr)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        text = f.read()
    with open(dst, "w") as f:
        f.write(clean(text))


if __name__ == "__main__":
    main()
