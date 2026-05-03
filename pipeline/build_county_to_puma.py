"""Derive county→PUMA crosswalk from the 2020 Census tract-to-PUMA file.

Reads pipeline/2020_Census_Tract_to_2020_PUMA.txt (tract-level mapping, ~85k
rows) and aggregates up to county level. For each county, emits a list of
{state, puma, weight} entries where weight is the share of the county's
tracts that fall in that PUMA.

This is a TRACT-COUNT proxy for population weighting. Most US counties are
either (a) wholly inside one PUMA (rural / sparse, common for multi-county
PUMAs) or (b) split across multiple PUMAs that all sit inside the county
(urban). In case (a) the proxy is exact (the only PUMA gets weight 1.0).
In case (b) it's an MVP approximation — proper population weighting from
ACS 5-year tract-level totals can replace it without changing the schema.

Connecticut special case: in June 2022 Census replaced CT's 8 traditional
counties with 9 Planning Regions as the official county-equivalent unit.
The master tract→PUMA file still keys CT tracts to old county FIPS (001
through 015). To match the topology and the v0.9 ACS-aggregate pipeline
which both adopted the new Planning Regions, we drop those rows and
substitute new ones built from the Census 2022 reference files staged
alongside (acs22_cousub22_tract22_st09.txt + acs22_cousub22_puma520_st09.txt).
The output keys CT under the new Planning Region GEOIDs (110–190).

Output: pipeline/county_to_puma.json — keyed by 5-digit county GEOID
(STATEFP+COUNTYFP). Used by run_pums_county.py to map per-PUMA lived FLY
aggregates to per-county rows for the county_lived_fly table.

Run from project root:
  python3 pipeline/build_county_to_puma.py
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict


HERE = os.path.dirname(__file__)
SRC = os.path.join(HERE, "2020_Census_Tract_to_2020_PUMA.txt")
CT_COUSUB_TRACT = os.path.join(HERE, "acs22_cousub22_tract22_st09.txt")
CT_COUSUB_PUMA  = os.path.join(HERE, "acs22_cousub22_puma520_st09.txt")
OUT = os.path.join(HERE, "county_to_puma.json")


def build_ct_tract_puma_rows() -> list[tuple[str, str, str, str]]:
    """Build CT tract→PUMA rows under the new Planning Region GEOIDs.

    Joins acs22_cousub22_tract22_st09 (gives us the 2022 tract GEOIDs that
    encode Planning Region county FIPS in positions 3–5) with
    acs22_cousub22_puma520_st09 (gives us each cousub's dominant PUMA by
    AREALAND_PART). Returns (statefp, countyfp, tractce, puma5ce) tuples
    matching the master file's row format, ready to substitute for the
    old-CT-FIPS rows.
    """
    if not (os.path.exists(CT_COUSUB_TRACT) and os.path.exists(CT_COUSUB_PUMA)):
        print("[county-puma] CT 2022 reference files missing; CT will use "
              "stale old-county-FIPS rows from the master file. To fix, "
              "fetch from https://www2.census.gov/geo/docs/maps-data/data/rel2022/")
        return []
    # cousub → dominant PUMA (by AREALAND_PART)
    cousub_to_puma: dict[str, tuple[str, int]] = {}
    with open(CT_COUSUB_PUMA, encoding="utf-8-sig") as f:
        header = f.readline().rstrip("\n").split("|")
        i_cousub = header.index("GEOID_COUSUB_22")
        i_puma   = header.index("GEOID_PUMA5_20")
        i_area   = header.index("AREALAND_PART")
        for line in f:
            cols = line.rstrip("\n").split("|")
            cousub = cols[i_cousub]
            puma   = cols[i_puma]
            area   = int(cols[i_area]) if cols[i_area].isdigit() else 0
            cur = cousub_to_puma.get(cousub)
            if cur is None or area > cur[1]:
                cousub_to_puma[cousub] = (puma, area)
    # cousub_tract → emit (state, planning-region, tract, puma5)
    out: list[tuple[str, str, str, str]] = []
    seen_tracts: set[str] = set()
    with open(CT_COUSUB_TRACT, encoding="utf-8-sig") as f:
        header = f.readline().rstrip("\n").split("|")
        i_cousub = header.index("GEOID_COUSUB_22")
        i_tract  = header.index("GEOID_TRACT_22")
        for line in f:
            cols = line.rstrip("\n").split("|")
            cousub  = cols[i_cousub]
            tract11 = cols[i_tract]
            if tract11 in seen_tracts:
                continue
            puma_full = cousub_to_puma.get(cousub)
            if not puma_full:
                continue
            puma5 = puma_full[0][-5:]    # last 5 digits of the 7-digit PUMA GEOID
            statefp  = tract11[:2]
            countyfp = tract11[2:5]      # new Planning Region: 110/120/.../190
            tractce  = tract11[5:]
            out.append((statefp, countyfp, tractce, puma5))
            seen_tracts.add(tract11)
    return out


def main() -> int:
    if not os.path.exists(SRC):
        raise SystemExit(f"missing {SRC} — fetch from "
                         f"https://www2.census.gov/geo/docs/reference/puma2020/")

    # (state_fips, county_fips) -> {puma_5_digit: tract_count}
    county_to_pumas: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_rows = 0
    n_ct_dropped = 0
    with open(SRC, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sfips = row["STATEFP"]
            cfips = row["COUNTYFP"]
            puma = row["PUMA5CE"]
            # Drop CT rows from the master file — they key to the old county
            # FIPS that Census retired in June 2022. We'll re-add CT rows
            # below from the post-restructure reference files.
            if sfips == "09":
                n_ct_dropped += 1
                continue
            county_to_pumas[(sfips, cfips)][puma] += 1
            n_rows += 1

    # Add CT rows under the new Planning Region GEOIDs (110–190).
    ct_rows = build_ct_tract_puma_rows()
    for sfips, cfips, _tractce, puma in ct_rows:
        county_to_pumas[(sfips, cfips)][puma] += 1
    n_rows += len(ct_rows)
    print(f"[county-puma] dropped {n_ct_dropped:,} stale CT rows; "
          f"added {len(ct_rows):,} CT rows under new Planning Region GEOIDs")

    # Build the crosswalk JSON. Sort PUMAs per county by descending weight so
    # the dominant PUMA is always first — useful for fast-path "wholly inside
    # one PUMA" detection in the consumer pipeline.
    out: dict[str, list[dict]] = {}
    n_split = 0
    for (sfips, cfips), pumas in sorted(county_to_pumas.items()):
        total = sum(pumas.values())
        geoid = sfips + cfips
        ranked = sorted(pumas.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > 1:
            n_split += 1
        out[geoid] = [
            {"state": sfips, "puma": puma, "weight": round(count / total, 6)}
            for puma, count in ranked
        ]

    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)

    print(f"[county-puma] read {n_rows:,} tract rows")
    print(f"[county-puma] mapped {len(out):,} counties")
    print(f"[county-puma] {n_split:,} split across multiple PUMAs "
          f"({n_split / len(out) * 100:.1f}%)")
    print(f"[county-puma] wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
