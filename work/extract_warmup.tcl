source $env(PDKPATH)/libs.tech/magic/sky130A.magicrc

# Read the standard-cell LEF (abstract pins + boundary only, no internal
# transistor geometry) BEFORE reading the GDS. This pre-defines each
# sky130_fd_sc_hd__* cell as an opaque black box, so when gds read encounters
# a matching structure name it keeps the LEF abstract instead of loading the
# full transistor-level .mag layout -- extraction then stops at the gate
# boundary instead of descending into individual FETs.
lef read /Users/quinn/Desktop/asic-puzzle-2026-master/pdk/sky130_fd_sc_hd.tlef
lef read /Users/quinn/Desktop/asic-puzzle-2026-master/pdk/sky130_fd_sc_hd.lef

# Snapshot cells defined by the LEF library so we can tell them apart from
# whatever the GDS itself introduces.
set lef_cells [cellname list allcells]

# Without this, Magic MERGES the GDS structure's geometry into the
# already-defined (LEF abstract) cell instead of skipping it, defeating the
# whole point of pre-reading the LEF -- we'd still get full transistor-level
# extraction inside every standard cell.
gds noduplicates yes

gds read /Users/quinn/Desktop/asic-puzzle-2026-master/warmup/04_final.gds

# The real design top is a cell that (a) is top-level (not instantiated by
# anything else) and (b) was NOT already defined by the LEF read.
set post_top [cellname list top]
set topcell ""
foreach c $post_top {
    if {$c eq "(UNNAMED)"} { continue }
    if {[lsearch -exact $lef_cells $c] == -1} { set topcell $c }
}
if {$topcell eq ""} {
    puts "ERROR: could not identify design top cell among: $post_top"
    exit 1
}
puts "TOP CELL: $topcell"
load $topcell

extract path /Users/quinn/Desktop/asic-puzzle-2026-master/work/warmup_extract
extract no capacitance
extract no coupling
extract unique
extract all

ext2spice lvs
ext2spice hierarchy on
ext2spice format ngspice
ext2spice -p /Users/quinn/Desktop/asic-puzzle-2026-master/work/warmup_extract -o /Users/quinn/Desktop/asic-puzzle-2026-master/work/warmup_extract/${topcell}.spice

puts "EXTRACTION_DONE topcell=$topcell"
quit -noprompt
