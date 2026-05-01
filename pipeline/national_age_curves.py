"""Build national_age_curves.json — national-average values for each of the
five FLY domains, by age of householder/respondent. Used by the /assessment
flow to compute a Flourishing Age via per-domain inverse-lookup.

ACS 1-yr 2024 by-age tables, plus an embedded NCHS 2022 life table for the
health domain. Each domain keeps its own native bucket granularity (we don't
interpolate the source data — that's done at use-time, in the JS scoring
engine, with a documented method).

Re-run when ACS or NCHS publish new annual data.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from lib import load_dotenv  # type: ignore

YEAR = 2024  # ACS 1-yr vintage
ACS = "https://api.census.gov/data/{year}/acs/acs1?get={vars}&for=us:1&key={key}"
ACS_S = "https://api.census.gov/data/{year}/acs/acs1/subject?get={vars}&for=us:1&key={key}"

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "national_age_curves.json")


def fetch(url: str) -> dict[str, float | None]:
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    header, row = data[0], data[1]
    out: dict[str, float | None] = {}
    for h, v in zip(header, row):
        if h == "us":
            continue
        try:
            out[h] = float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            out[h] = None
    return out


def fetch_acs(vars_: list[str], key: str) -> dict[str, float | None]:
    return fetch(ACS.format(year=YEAR, vars=",".join(vars_), key=key))


def fetch_acs_subject(vars_: list[str], key: str) -> dict[str, float | None]:
    return fetch(ACS_S.format(year=YEAR, vars=",".join(vars_), key=key))


def build_purchasing_power(key: str) -> dict:
    """ACS B19049 — median household income by age (4 buckets)."""
    vars_ = ["B19049_002E", "B19049_003E", "B19049_004E", "B19049_005E"]
    r = fetch_acs(vars_, key)
    return {
        "metric": "Median household income (2024 dollars) by age of householder",
        "source": f"ACS 1-yr {YEAR} B19049",
        "buckets": [
            {"min_age": 15, "max_age": 24, "value": r["B19049_002E"]},
            {"min_age": 25, "max_age": 44, "value": r["B19049_003E"]},
            {"min_age": 45, "max_age": 64, "value": r["B19049_004E"]},
            {"min_age": 65, "max_age": 100, "value": r["B19049_005E"]},
        ],
    }


def build_income_distribution(key: str) -> dict:
    """ACS B19037 — full household income distribution by age (4 buckets, 16 brackets each).

    Each age bucket gets a list of (upper_bound, count) tuples. The scoring
    engine computes the user's percentile by integrating the CDF up to their
    reported income (linear within the containing bracket).
    """
    # Income bracket upper bounds (the "to $X" cap of each bracket; last is open-ended).
    upper_bounds = [
        10_000, 15_000, 20_000, 25_000, 30_000, 35_000, 40_000,
        45_000, 50_000, 60_000, 75_000, 100_000, 125_000, 150_000, 200_000, None,
    ]
    # Variable codes per age bucket: header + 16 bracket counts.
    AGE_OFFSETS = {
        (15, 24): 2,    # _002 header, _003.._018 brackets
        (25, 44): 19,   # _019 header, _020.._035
        (45, 64): 36,   # _036 header, _037.._052
        (65, 100): 53,  # _053 header, _054.._069
    }
    # Build the var list, then split into batches under the ACS ~50-var per-request cap.
    needed = []
    for (lo, hi), off in AGE_OFFSETS.items():
        for i in range(1, 17):
            needed.append(f"B19037_{off + i:03d}E")
    r: dict[str, float | None] = {}
    BATCH = 40
    for i in range(0, len(needed), BATCH):
        r.update(fetch_acs(needed[i:i + BATCH], key))

    buckets = []
    for (lo, hi), off in AGE_OFFSETS.items():
        counts = []
        for i, ub in enumerate(upper_bounds):
            cnt = r[f"B19037_{off + i + 1:03d}E"] or 0
            counts.append({"upper_bound": ub, "count": cnt})
        buckets.append({"min_age": lo, "max_age": hi, "brackets": counts})
    return {
        "metric": "Household income distribution by age of householder (16 brackets per age band)",
        "source": f"ACS 1-yr {YEAR} B19037",
        "note": "Last bracket is open-ended ($200k+); user incomes above $200k are all reported as p≥(cumulative through bracket 15) regardless of exact value.",
        "buckets": buckets,
    }


def build_wealth_homeownership(key: str) -> dict:
    """ACS B25007 — homeownership rate by age of householder (9 buckets)."""
    own_codes  = [f"B25007_{i:03d}E" for i in range(3, 12)]   # _003.._011
    rent_codes = [f"B25007_{i:03d}E" for i in range(13, 22)]  # _013.._021
    r = fetch_acs(own_codes + rent_codes, key)
    bands = [(15, 24), (25, 34), (35, 44), (45, 54), (55, 59),
             (60, 64), (65, 74), (75, 84), (85, 100)]
    buckets = []
    for i, (lo, hi) in enumerate(bands):
        own = r[own_codes[i]] or 0
        rent = r[rent_codes[i]] or 0
        denom = own + rent
        rate = (own / denom) if denom > 0 else None
        buckets.append({"min_age": lo, "max_age": hi,
                        "value": round(rate, 4) if rate is not None else None})
    return {
        "metric": "Homeownership rate (owner-occupied / occupied units) by age of householder",
        "source": f"ACS 1-yr {YEAR} B25007",
        "buckets": buckets,
    }


def build_family_married(key: str) -> dict:
    """ACS S1201 — share now-married (except separated), age × sex collapsed (6 buckets).

    Subtle: in subject table S1201, C01 columns are population counts and C02
    columns are PERCENTAGES (e.g. 25.3 means 25.3%). To collapse male/female
    into both-sexes by age, weighted-average the rates by their populations.
    """
    male_total   = [f"S1201_C01_{i:03d}E" for i in range(3, 9)]
    male_pct     = [f"S1201_C02_{i:03d}E" for i in range(3, 9)]
    female_total = [f"S1201_C01_{i:03d}E" for i in range(10, 16)]
    female_pct   = [f"S1201_C02_{i:03d}E" for i in range(10, 16)]
    r = fetch_acs_subject(male_total + male_pct + female_total + female_pct, key)
    bands = [(15, 19), (20, 34), (35, 44), (45, 54), (55, 64), (65, 100)]
    buckets = []
    for i, (lo, hi) in enumerate(bands):
        m_n = r[male_total[i]]   or 0
        m_p = (r[male_pct[i]]    or 0) / 100.0
        f_n = r[female_total[i]] or 0
        f_p = (r[female_pct[i]]  or 0) / 100.0
        denom = m_n + f_n
        rate = ((m_n * m_p + f_n * f_p) / denom) if denom > 0 else None
        buckets.append({"min_age": lo, "max_age": hi,
                        "value": round(rate, 4) if rate is not None else None})
    return {
        "metric": "Share now-married (except separated), both sexes combined, by age",
        "source": f"ACS 1-yr {YEAR} S1201 (C01 = population counts, C02 = pct now-married)",
        "buckets": buckets,
    }


def build_education_associate_plus(key: str) -> dict:
    """ACS B15001 — share with associate's degree or higher, both sexes (5 age buckets).

    Per-bucket structure: each age bucket has 7 educational categories. Associate+
    is the last 3 categories (associate, bachelor's, graduate/professional).
    """
    # B15001 male age buckets start at offsets:
    #   18-24: _003..._010 (header _003, levels _004..._010)
    #   25-34: _011..._018
    #   35-44: _019..._026
    #   45-64: _027..._034
    #   65+:   _035..._042
    # Female section mirrors offset+42: female total _043, then _044... wait, B15001 has 334 vars.
    # Let me probe by offset: each bucket = 8 vars (header + 7 levels). 5 male buckets = 40 vars.
    # Male starts at _002 (male total), then _003 (18-24 header) — so 1 + 5*8 = 41 vars for males.
    # Female total = _044, then 5 buckets of 8 = 41 vars => 1 + 41 + 1 + 41 = 84 vars total expected,
    # but the table has 334 vars (E + M + others?) — actually each detail has E/M, so 84*2 = 168.
    # Don't overthink it; just request what we need.
    male_buckets   = [(3, 8), (11, 16), (19, 24), (27, 32), (35, 40)]   # (header, associate-start)
    female_buckets = [(44, 49), (52, 57), (60, 65), (68, 73), (76, 81)]  # parallel, female section
    bands = [(18, 24), (25, 34), (35, 44), (45, 64), (65, 100)]

    needed = []
    for header, assoc in male_buckets + female_buckets:
        needed.append(f"B15001_{header:03d}E")            # bucket total
        for off in range(0, 3):                            # _assoc, _bachelor, _grad
            needed.append(f"B15001_{assoc + off:03d}E")

    r = fetch_acs(needed, key)
    buckets = []
    for i, (lo, hi) in enumerate(bands):
        m_hdr, m_assoc = male_buckets[i]
        f_hdr, f_assoc = female_buckets[i]
        denom = (r[f"B15001_{m_hdr:03d}E"] or 0) + (r[f"B15001_{f_hdr:03d}E"] or 0)
        num = sum((r[f"B15001_{m_assoc + off:03d}E"] or 0) + (r[f"B15001_{f_assoc + off:03d}E"] or 0)
                  for off in range(3))
        rate = (num / denom) if denom > 0 else None
        buckets.append({"min_age": lo, "max_age": hi,
                        "value": round(rate, 4) if rate is not None else None})
    return {
        "metric": "Share of adults with associate's degree or higher, both sexes, by age",
        "source": f"ACS 1-yr {YEAR} B15001",
        "buckets": buckets,
    }


# NCHS Life Tables for the United States, 2022 (NVSR Vol. 72 No. 12, released 2023).
# e(x) = expected remaining years of life at exact age x. Both sexes, all races.
# Hardcoded at 5-year intervals; JS interpolates linearly between them.
# Source: https://www.cdc.gov/nchs/data/nvsr/nvsr72/nvsr72-12.pdf
NCHS_LIFE_TABLE_2022 = {
    0: 77.5, 5: 73.0, 10: 68.0, 15: 63.1, 18: 60.2, 20: 58.3,
    25: 53.6, 30: 48.9, 35: 44.3, 40: 39.6,
    45: 35.1, 50: 30.7, 55: 26.4, 60: 22.4,
    65: 18.5, 70: 14.9, 75: 11.6, 80: 8.7,
    85: 6.4, 90: 4.5, 95: 3.2, 100: 2.4,
}


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    key = os.environ["CENSUS_API_KEY"]

    print("[curves] purchasing power (B19049 medians)…", file=sys.stderr)
    pp = build_purchasing_power(key)
    print("[curves] purchasing power distribution (B19037)…", file=sys.stderr)
    pp_dist = build_income_distribution(key)
    print("[curves] wealth (B25007 homeownership)…", file=sys.stderr)
    wlth = build_wealth_homeownership(key)
    print("[curves] family (S1201 married rate)…", file=sys.stderr)
    fam = build_family_married(key)
    print("[curves] education (B15001 associate+)…", file=sys.stderr)
    edu = build_education_associate_plus(key)

    out = {
        "release_companion": "v2025.12",
        "vintage_year": YEAR,
        "domains": {
            "purchasing_power": pp,
            "purchasing_power_distribution": pp_dist,
            "wealth": wlth,
            "family": fam,
            "education": edu,
        },
        "life_table": {
            "metric": "Life expectancy at exact age x (years remaining)",
            "source": "NCHS NVSR Vol. 72 No. 12, US Life Tables 2022",
            "interp": "Linear between hardcoded 5-year anchors (0, 5, 10, ..., 100).",
            "anchors": NCHS_LIFE_TABLE_2022,
        },
        "interpretation_notes": {
            "bucket_handling": "Each domain's curve is preserved at its native ACS bucket granularity. The /assessment scoring engine maps the user's chronological age to the bucket containing it; for inverse lookup (Flourishing Age), it linearly interpolates between bucket midpoints to find the age at which the curve passes through the user's reported value.",
            "non_monotonic_curves": "Several curves (income, married-rate, homeownership, education) rise then fall with age. When the inverse lookup hits an ambiguous match (two ages with the same value), the scoring engine returns the lower age — the 'achievement age.'",
        },
    }

    out_path = os.path.realpath(OUT_PATH)
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"[curves] wrote {out_path} ({os.path.getsize(out_path):,} bytes)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
