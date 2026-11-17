#!/usr/bin/env python3
"""Sanity-check exported STLs for 3D printing.

For every STL given (or all under pod/stl and brain/stl):
  * bounding box, in the print orientation (Z = build direction)
  * watertight: every triangle edge is shared by exactly two triangles
  * sits on the bed: min Z is 0 and there is real contact area
  * overhangs: area of downward-facing surface steeper than --max-overhang
    degrees from vertical (default 45°, the usual no-support limit), ignoring
    the bed itself and short bridges (horizontal faces narrower than --bridge).

Exit status 1 if any part fails. No third-party modules needed.
"""
import argparse, glob, math, os, struct, sys
from collections import Counter


def read_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    tris = []
    if data[:5] != b"solid" or len(data) >= 84 and struct.unpack_from("<I", data, 80)[0] * 50 + 84 == len(data):
        n = struct.unpack_from("<I", data, 80)[0]
        for i in range(n):
            vals = struct.unpack_from("<12f", data, 84 + 50 * i)
            tris.append((vals[0:3], vals[3:6], vals[6:9], vals[9:12]))
    else:
        verts, normal = [], None
        for line in data.decode("ascii", "replace").splitlines():
            w = line.split()
            if not w:
                continue
            if w[0] == "facet":
                normal = tuple(map(float, w[2:5]))
            elif w[0] == "vertex":
                verts.append(tuple(map(float, w[1:4])))
                if len(verts) == 3:
                    tris.append((normal, *verts))
                    verts = []
    return tris


def face_normal(a, b, c):
    ux, uy, uz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
    vx, vy, vz = c[0]-a[0], c[1]-a[1], c[2]-a[2]
    nx, ny, nz = uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx
    area2 = math.sqrt(nx*nx+ny*ny+nz*nz)
    if area2 == 0:
        return (0, 0, 0), 0
    return (nx/area2, ny/area2, nz/area2), area2/2


def check(path, max_overhang, bridge):
    tris = read_stl(path)
    xs = [v[i] for t in tris for v in t[1:] for i in (0,)]
    ys = [v[1] for t in tris for v in t[1:]]
    zs = [v[2] for t in tris for v in t[1:]]
    bbox = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
    zmin = bbox[4]

    # watertightness
    edges = Counter()
    for _, a, b, c in tris:
        for p, q in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((p, q)))] += 1
    bad_edges = sum(1 for v in edges.values() if v != 2)

    # overhang / bed contact
    limit = math.cos(math.radians(90 - max_overhang - 0.5))   # nz below -limit => too steep (0.5° tolerance)
    bed_area = overhang_area = bridge_area = hole_area = 0.0
    total_down = 0.0
    for _, a, b, c in tris:
        n, area = face_normal(a, b, c)
        if area == 0:
            continue
        if n[2] < -0.999 and max(a[2], b[2], c[2]) < zmin + 1e-3:
            bed_area += area
            continue
        if n[2] < 0:
            total_down += area
        if n[2] < -0.999:
            # flat ceiling: a bridge or a true overhang. Width = shortest side of the triangle
            # is a crude proxy; small flat patches are the tops of holes/slots and bridge fine.
            w = min(math.dist(a, b), math.dist(b, c), math.dist(c, a))
            if w <= bridge:
                bridge_area += area
            else:
                overhang_area += area
        elif n[2] < -limit:
            # faces with a sub-millimetre edge are facets of a round horizontal hole
            # (screw / insert holes): their tops sag slightly but print fine
            if min(math.dist(a, b), math.dist(b, c), math.dist(c, a)) < 1.0:
                hole_area += area
            else:
                overhang_area += area

    ok = bad_edges == 0 and abs(zmin) < 1e-3 and bed_area > 20 and overhang_area < 1.0
    name = os.path.relpath(path)
    print(f"{name:34s} {len(tris):6d} tris  bbox {bbox[1]-bbox[0]:6.1f} x {bbox[3]-bbox[2]:6.1f} x {bbox[5]-bbox[4]:5.1f} mm"
          f"  zmin {zmin:5.2f}  bed {bed_area:7.0f} mm2  flat-bridge {bridge_area:5.0f} mm2"
          f"  >{max_overhang}° overhang {overhang_area:6.1f} mm2 (+{hole_area:4.0f} in round holes)  open edges {bad_edges}  {'OK' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--max-overhang", type=float, default=45.0)
    ap.add_argument("--bridge", type=float, default=16.0, help="flat ceilings narrower than this count as bridges")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    files = a.files or sorted(glob.glob(os.path.join(here, "*", "stl", "*.stl")))
    results = [check(f, a.max_overhang, a.bridge) for f in files]
    sys.exit(0 if all(results) else 1)
