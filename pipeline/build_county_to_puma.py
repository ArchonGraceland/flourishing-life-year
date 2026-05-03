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
OUT = os.path.join(HERE, "county_to_puma.json")


def main() -> int:
    if not os.path.exists(SRC):
        raise SystemExit(f"missing {SRC} — fetch from "
                         f"https://www2.census.gov/geo/docs/reference/puma2020/")

    # (state_fips, county_fips) -> {puma_5_digit: tract_count}
    county_to_pumas: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_rows = 0
    with open(SRC, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sfips = row["STATEFP"]
            cfips = row["COUNTYFP"]
            puma = row["PUMA5CE"]
            county_to_pumas[(sfips, cfips)][puma] += 1
            n_rows += 1

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
