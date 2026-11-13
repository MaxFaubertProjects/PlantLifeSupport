#!/usr/bin/env bash
# Render every STL and preview PNG for both enclosures.
# Usage: ./build.sh            (from hardware/enclosures)
set -euo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
render() {  # render <dir> <scadfile> <part>
  echo "  $1/stl/$3.stl"
  "$OPENSCAD" --backend=manifold --export-format=binstl -q -o "$1/stl/$3.stl" -D "part=\"$3\"" "$1/$2"
}
preview() {  # preview <dir> <scadfile> <part> <png> [camera]
  echo "  $1/$4"
  "$OPENSCAD" -q -o "$1/$4" -D "part=\"$3\"" --imgsize=1600,1200 --viewall --autocenter \
    --colorscheme=Tomorrow --projection=p ${5:+--camera=$5} "$1/$2"
}
echo "pod"
for p in base lid stake cam_arm cam_cradle; do render pod pod.scad $p; done
preview pod pod.scad assembly  preview_assembly.png  "0,0,0,55,0,35,0"
preview pod pod.scad print_all preview_print_plate.png
echo "brain"
for p in box lid pump_bracket; do render brain brain.scad $p; done
preview brain brain.scad assembly  preview_assembly.png  "0,0,0,55,0,215,0"
preview brain brain.scad print_all preview_print_plate.png
echo done
