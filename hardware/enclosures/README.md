# Enclosures

3D-printable housings for the two boards in [docs/HARDWARE_V2.md](../../docs/HARDWARE_V2.md).

| Folder | What | Parts |
|---|---|---|
| [`pod/`](pod/README.md) | **Sensor pod** on a stake in the pot | base, lid, printed stake, camera arm, camera cradle |
| [`brain/`](brain/README.md) | **Brain box** for the Pi 5 + HAT, by the reservoir | box, lid, pump bracket |

Each folder has one `.scad` source, a `stl/` folder with every part already
exported in its print orientation, and two preview PNGs (assembly and print plate).

## Sending to a print service

Upload the files in `*/stl/`. Each STL is already lying the way it should be
printed (Z up = build direction) and none needs supports. Per-part material,
layer height and notes are in the folder READMEs. Short version:

| | Material | Layer | Infill | Supports |
|---|---|---|---|---|
| Pod (all parts) | PETG or ASA (lives next to a misted plant) | 0.2 mm | 25 % | none |
| Brain (all parts) | PETG (PLA is fine indoors) | 0.2 mm | 20 % | none, brim on nothing |

## Editing the designs

The sources are OpenSCAD (free, [openscad.org](https://openscad.org); the
2025+ "snapshot" build is much faster than the 2021 release). Open the `.scad`
file, change a number in the **PARAMETERS** block at the top, press F5 to
preview and F6 to render. Every parameter has a comment saying what it does.
Set `part = "base"` (or whichever part) at the top of the file to look at one
part, `"assembly"` to see everything together.

The PCB sizes and hole positions are parameters (the real boards in
`hardware/pod/` and `hardware/brain/` did not exist when these were drawn).
When the KiCad boards are final, copy their outline size, mounting-hole
positions and connector positions into the PARAMETERS block and rebuild.

## Rebuilding the STLs and previews

```bash
./build.sh
```

renders every part with `openscad --backend=manifold` (seconds per part) and
the preview images. Then

```bash
python3 check_stl.py
```

reads every STL back and checks it is watertight, sits flat on the bed, and has
no downward-facing surface steeper than 45° (the no-supports limit), reporting
the tops of small round horizontal holes separately since those print fine.
