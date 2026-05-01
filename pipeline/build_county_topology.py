"""Build /county_topology.json from Census Cartographic Boundary 2023 (5m).

Pure stdlib — no GDAL, no pyshp. Downloads cb_2023_us_county_5m.zip and
cb_2023_us_state_5m.zip, parses the shapefile + dbf, applies Douglas-Peucker
simplification, drops territories per spec Q1, and writes:

    {
      "vintage": "Census CB 2023 5m",
      "counties": GeoJSON FeatureCollection,
      "states":   GeoJSON FeatureCollection
    }

The map page (/map.html) loads this fixture instead of unpkg us-atlas, which
has the pre-2022 Connecticut county FIPS that don't match the new CT
planning regions used by ACS/BEA from 2022 onward.

Re-run when Census publishes a new CB vintage (typically annual).
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import urllib.request
import zipfile

CB_BASE = "https://www2.census.gov/geo/tiger/GENZ2023/shp/"
COUNTY_ZIP = "cb_2023_us_county_5m.zip"
STATE_ZIP  = "cb_2023_us_state_5m.zip"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "county_topology.json")
NAMES_PATH = os.path.join(os.path.dirname(__file__), "..", "geoid_names.json")

EXCLUDED_STATEFP = {"60", "66", "69", "72", "78"}  # AS, GU, MP, PR, VI per Q1

# Simplification tolerance in degrees. 0.008 ≈ 800 m at the equator —
# imperceptible at county-overview scale and brings the output to ~1 MB.
SIMPLIFY_EPS_COUNTY = 0.008
SIMPLIFY_EPS_STATE = 0.012


def fetch_zip(name: str) -> bytes:
    print(f"  fetching {name}…", file=sys.stderr)
    return urllib.request.urlopen(CB_BASE + name).read()


def parse_dbf(data: bytes) -> list[dict]:
    n_recs = struct.unpack("<I", data[4:8])[0]
    header_size = struct.unpack("<H", data[8:10])[0]
    record_size = struct.unpack("<H", data[10:12])[0]
    fields = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b'\x00', 1)[0].decode('ascii')
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fields.append((name, ftype, flen))
        pos += 32
    out = []
    pos = header_size
    for _ in range(n_recs):
        if pos + 1 > len(data):
            break
        if data[pos:pos + 1] == b'*':
            pos += record_size
            continue
        rec = {}
        offset = pos + 1
        for name, _ftype, flen in fields:
            rec[name] = data[offset:offset + flen].rstrip().decode('latin-1', 'replace').strip()
            offset += flen
        out.append(rec)
        pos += record_size
    return out


def parse_shp(data: bytes) -> list[list[list[tuple[float, float]]] | None]:
    shapes = []
    pos = 100
    while pos < len(data):
        if pos + 8 > len(data):
            break
        rec_num, content_len = struct.unpack(">II", data[pos:pos + 8])
        pos += 8
        end = pos + content_len * 2
        if pos + 4 > len(data):
            break
        shape_type = struct.unpack("<I", data[pos:pos + 4])[0]
        inner = pos + 4
        if shape_type == 0:
            shapes.append(None)
        elif shape_type == 5:  # Polygon
            inner += 32  # bbox 4 doubles
            n_parts, n_points = struct.unpack("<II", data[inner:inner + 8])
            inner += 8
            parts = list(struct.unpack(f"<{n_parts}I", data[inner:inner + 4 * n_parts]))
            inner += 4 * n_parts
            pts = struct.unpack(f"<{2 * n_points}d", data[inner:inner + 16 * n_points])
            rings = []
            for i, start in enumerate(parts):
                stop = parts[i + 1] if i + 1 < n_parts else n_points
                rings.append([(pts[2 * j], pts[2 * j + 1]) for j in range(start, stop)])
            shapes.append(rings)
        else:
            shapes.append(None)
        pos = end
    return shapes


def signed_area(ring) -> float:
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += (x2 - x1) * (y2 + y1)
    return a / 2


def rings_to_geometry(rings):
    """Group shapefile rings (CW outer, CCW holes) into Polygon/MultiPolygon."""
    if not rings:
        return None
    polygons = []
    current = None
    for ring in rings:
        if signed_area(ring) > 0:  # outer in shapefile convention
            if current is not None:
                polygons.append(current)
            current = [list(ring)]
        else:
            if current is None:
                current = [list(ring)]
            else:
                current.append(list(ring))
    if current is not None:
        polygons.append(current)
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def perpendicular_distance(p, a, b) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    fx, fy = ax + t * dx, ay + t * dy
    return ((px - fx) ** 2 + (py - fy) ** 2) ** 0.5


def douglas_peucker(points, eps: float):
    """Iterative DP — recursion overflows on long rings (e.g. coastal)."""
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax = 0.0
        idx = -1
        a = points[i]
        b = points[j]
        for k in range(i + 1, j):
            d = perpendicular_distance(points[k], a, b)
            if d > dmax:
                dmax = d
                idx = k
        if dmax > eps:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [points[k] for k in range(n) if keep[k]]


def simplify_ring(r, eps):
    s = douglas_peucker(r, eps)
    if len(s) < 4:  # closed polygon ring needs ≥4 points (first == last)
        return r
    return s


def simplify_geometry(geom, eps):
    if geom is None:
        return None
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [simplify_ring(r, eps) for r in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[simplify_ring(r, eps) for r in poly] for poly in geom["coordinates"]]}
    return geom


def round_geom(geom, decimals: int = 5):
    """Round coordinates to N decimals (≈ 1 m at equator with 5; ≈ 11 m with 4).

    Smaller numbers serialize shorter — meaningful payload reduction.
    """
    if geom is None:
        return None
    def rr(r):
        return [[round(x, decimals), round(y, decimals)] for (x, y) in r]
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rr(r) for r in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[rr(r) for r in poly] for poly in geom["coordinates"]]}
    return geom


def build_features(shp_data, dbf_data, geoid_field, name_field, statefp_field, eps):
    shapes = parse_shp(shp_data)
    records = parse_dbf(dbf_data)
    features = []
    for shape, rec in zip(shapes, records):
        if shape is None:
            continue
        if rec.get(statefp_field, "") in EXCLUDED_STATEFP:
            continue
        geom = rings_to_geometry(shape)
        geom = simplify_geometry(geom, eps)
        geom = round_geom(geom, 5)
        features.append({
            "type": "Feature",
            "id": rec[geoid_field],
            "properties": {
                "GEOID": rec[geoid_field],
                "NAME": rec[name_field],
                "STATEFP": rec[statefp_field],
            },
            "geometry": geom,
        })
    return features


def main() -> int:
    print("[topology] downloading Census CB 2023 5m …", file=sys.stderr)
    cz = fetch_zip(COUNTY_ZIP)
    sz = fetch_zip(STATE_ZIP)

    print("[topology] parsing county shapefile…", file=sys.stderr)
    czf = zipfile.ZipFile(io.BytesIO(cz))
    cshp = czf.read("cb_2023_us_county_5m.shp")
    cdbf = czf.read("cb_2023_us_county_5m.dbf")
    cf = build_features(cshp, cdbf, "GEOID", "NAME", "STATEFP", SIMPLIFY_EPS_COUNTY)
    print(f"[topology]   {len(cf)} county features", file=sys.stderr)

    print("[topology] parsing state shapefile…", file=sys.stderr)
    szf = zipfile.ZipFile(io.BytesIO(sz))
    sshp = szf.read("cb_2023_us_state_5m.shp")
    sdbf = szf.read("cb_2023_us_state_5m.dbf")
    sf = build_features(sshp, sdbf, "GEOID", "NAME", "STATEFP", SIMPLIFY_EPS_STATE)
    print(f"[topology]   {len(sf)} state features", file=sys.stderr)

    out = {
        "vintage": "Census CB 2023 5m",
        "simplification_eps_deg": {"counties": SIMPLIFY_EPS_COUNTY, "states": SIMPLIFY_EPS_STATE},
        "counties": {"type": "FeatureCollection", "features": cf},
        "states":   {"type": "FeatureCollection", "features": sf},
    }
    out_path = os.path.realpath(OUT_PATH)
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"[topology] wrote {out_path} ({os.path.getsize(out_path):,} bytes)", file=sys.stderr)

    # Also emit a small geoid → "Name, ST" lookup so the home page can label
    # counties without pulling the full 2.3 MB topology.
    state_abbr = {"01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC","12":"FL",
                  "13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME",
                  "24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH",
                  "34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI",
                  "45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY"}
    names = {}
    for f in cf:
        geoid = f["properties"]["GEOID"]
        name = f["properties"]["NAME"]
        st = state_abbr.get(f["properties"]["STATEFP"], "")
        names[geoid] = f"{name}, {st}".rstrip(", ")
    names_path = os.path.realpath(NAMES_PATH)
    with open(names_path, "w") as f:
        json.dump(names, f, separators=(",", ":"))
    print(f"[topology] wrote {names_path} ({os.path.getsize(names_path):,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
