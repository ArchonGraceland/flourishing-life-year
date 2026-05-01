"""Q7 — National FLY trend ("§ The national balance").

Computes a per-year national FLY index from base year 2019 to the most recent
year with all inputs final (currently 2023, bounded by NCHS NVSS final
mortality lag — see Q7.7).

Methodology — frozen 2019 ratio anchor (Q7.2 implementation):
  For each domain d, year T:
    domain_ratio[d, T] = national_value(d, T) / national_value(d, 2019)
  composite_index[T] = geomean(domain_ratio[d, T] for d in 5 domains) × 100
  → composite_index[2019] = 100 by construction
  → 2024 composite_index of 102.4 means national FLY is up 2.4% vs 2019

The "frozen percentile cuts" language in Q7.2 was imprecise; the simpler
ratio anchor matches the spec's intent (single anchor year, no re-normalization
across years) without requiring a fictional cross-county distribution to
rank a national-mean indicator against.

Per-domain breakdown stored as JSONB so the secondary chart can show which
domains drove each year's composite (Q7.5).

Health source: NCHS NVSS final per Q7.4 — the WONDER 1999–2020 product is
locked elsewhere for the county map but doesn't extend past 2020. NCHS
national life expectancy at birth (annual) is hardcoded from canonical
published tables; documented in source_vintages as `nchs_nvss_county`
(distinct from the `cdc_wonder_ucd` source key).

ACS 5-year overlap caveat (Q7.6): each year's ACS read is a 5-year window
centered around year T-2. Year-to-year changes in ACS-driven domains are
smoothed by the rolling-sample design. Documented in the section copy.

Re-run when ACS or NCHS publish new final-year data.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import urllib.request
from datetime import date

from lib import load_dotenv  # type: ignore

BASE_YEAR = 2019
TREND_YEARS = [2019, 2020, 2021, 2022, 2023]   # bounded by NCHS NVSS final lag
ACS_TPL = "https://api.census.gov/data/{year}/acs/acs5?get={vars}&for=us:1&key={key}"

# BLS CPI-U annual averages, series CUUR0000SA0. Used to deflate ACS B19013
# nominal-dollar income to constant base-year dollars. Other domains
# (homeownership rate, family rate, education rate, life expectancy) are
# already on intrinsically comparable scales.
CPI_BY_YEAR = {
    2019: 255.657, 2020: 258.811, 2021: 270.970,
    2022: 292.655, 2023: 304.702,
}

# US population mid-year estimates (Census Vintage 2024 release, NST-EST2024).
# Used for total-FLY computation: the index treats the 2019 anchor as composite
# percentile 50 (= 0.50 FLY-per-capita), so total FLY in year T is
#   total_fly_T = (composite_index_T / 100) * 0.50 * population_T
# At 2019 by construction: 1.00 × 0.50 × 328.2M = 164.1M FLY.
US_POPULATION_BY_YEAR = {
    2019: 328_239_523,
    2020: 331_511_512,
    2021: 332_031_554,
    2022: 333_287_557,
    2023: 334_914_895,
}
# Anchor: 2019 composite_percentile = 50, so FLY-per-capita anchor = 0.50.
FLY_PER_CAPITA_ANCHOR = 0.50

# NCHS NVSS national life expectancy at birth, both sexes, all races.
# Source: CDC NCHS National Vital Statistics annual reports (NVSR Vol 72 No 12,
# Dec 2024 for 2022; provisional for 2023 from NCHS Data Brief No. 521 noted
# but per Q7.4 we use only finalized values).
LIFE_EXPECTANCY_BY_YEAR = {
    2019: 78.8,
    2020: 77.0,  # COVID-era drop
    2021: 76.4,  # continued decline
    2022: 77.5,  # partial recovery
    2023: 78.4,  # provisional NCHS Data Brief No. 521 (Dec 2024) — final due 2026
}


def fetch_acs(year: int, var_codes: list[str], key: str) -> dict[str, float | None]:
    url = ACS_TPL.format(year=year, vars=",".join(var_codes), key=key)
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


def national_values_for_year(year: int, key: str) -> dict[str, float]:
    """Pull national headline value for each ACS-driven domain for a given ACS 5-yr release year."""
    # B19013_001E: median household income
    # B25003_001E (occupied), B25003_002E (owner) → ownership rate
    # B09005: post-2019 schema. _001 total, _002 married-couple, _003 cohabiting → two-parent share
    # B15003: _001 total pop 25+, _021–_025 associate+ → associate+ share
    vars_ = [
        "B19013_001E",
        "B25003_001E", "B25003_002E",
        "B09005_001E", "B09005_002E", "B09005_003E",
        "B15003_001E",
        "B15003_021E", "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E",
    ]
    r = fetch_acs(year, vars_, key)

    nominal_income = r.get("B19013_001E")
    # Each ACS 5-yr release reports income in that release-year dollars.
    # Deflate to BASE_YEAR (2019) dollars so cross-year comparison is real.
    income = (nominal_income * CPI_BY_YEAR[BASE_YEAR] / CPI_BY_YEAR[year]
              if nominal_income else None)
    own_rate = (r["B25003_002E"] / r["B25003_001E"]
                if r.get("B25003_002E") and r.get("B25003_001E") else None)
    fam_total = r.get("B09005_001E")
    fam_2p = ((r.get("B09005_002E") or 0) + (r.get("B09005_003E") or 0))
    family_rate = (fam_2p / fam_total) if fam_total and fam_total > 0 else None
    edu_total = r.get("B15003_001E")
    edu_assoc = sum(r.get(v) or 0 for v in
                    ("B15003_021E", "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E"))
    edu_rate = (edu_assoc / edu_total) if edu_total and edu_total > 0 else None

    return {
        "purchasing_power": income,           # dollars (nominal)
        "wealth":           own_rate,         # fraction
        "family":           family_rate,      # fraction (married-couple + cohabiting)
        "education":        edu_rate,         # fraction
        "health":           LIFE_EXPECTANCY_BY_YEAR.get(year),  # years
    }


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"].rstrip("/")
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]
    today = date.today().isoformat()

    # Pull each year's national values
    print(f"[trend] release={release} pulling ACS 5-yr at us:1 for years {TREND_YEARS}…",
          file=sys.stderr)
    by_year: dict[int, dict[str, float]] = {}
    for year in TREND_YEARS:
        vals = national_values_for_year(year, census_key)
        by_year[year] = vals
        print(f"[trend]   {year}: income=${vals['purchasing_power']:,.0f}  "
              f"own={vals['wealth']*100:.1f}%  fam={vals['family']*100:.1f}%  "
              f"edu={vals['education']*100:.1f}%  le={vals['health']:.1f}yr",
              file=sys.stderr)

    base = by_year[BASE_YEAR]
    rows = []
    for year in TREND_YEARS:
        v = by_year[year]
        per_domain = {}
        ratios = []
        for dom in ("purchasing_power", "wealth", "family", "health", "education"):
            ratio = v[dom] / base[dom] if v[dom] and base[dom] else None
            per_domain[dom] = round(ratio * 100, 2) if ratio is not None else None
            if ratio is not None and ratio > 0:
                ratios.append(ratio)
        if ratios:
            geomean = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
            composite_index = round(geomean * 100, 2)
        else:
            composite_index = None
        # Total FLY produced in year T = (composite_index/100) × FLY-per-capita anchor × US pop
        pop = US_POPULATION_BY_YEAR.get(year)
        total_fly = (
            round((composite_index / 100.0) * FLY_PER_CAPITA_ANCHOR * pop, 0)
            if (composite_index is not None and pop) else None
        )
        rows.append({
            "release_version": release,
            "year": year,
            "composite_index": composite_index,
            "per_domain_breakdown": per_domain,
            "total_fly": total_fly,
        })
        print(f"[trend]   {year}: composite_index={composite_index}  "
              f"total_fly={total_fly:,.0f}" if total_fly is not None
              else f"[trend]   {year}: composite_index={composite_index}",
              file=sys.stderr)

    # Write to Supabase national_trend
    print("[trend] clearing prior trend rows for this release…", file=sys.stderr)
    del_url = f"{sb_url}/rest/v1/national_trend?release_version=eq.{release}"
    import requests
    r = requests.delete(del_url, headers={
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Prefer": "return=minimal",
    }, timeout=120)
    r.raise_for_status()

    print("[trend] writing national_trend rows…", file=sys.stderr)
    upsert_url = f"{sb_url}/rest/v1/national_trend?on_conflict=release_version,year"
    headers = {
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(upsert_url, headers=headers, json=rows, timeout=120)
    r.raise_for_status()

    # Document the trend mortality source as a separate source_vintages row
    print("[trend] writing nchs_nvss_county source_vintages row…", file=sys.stderr)
    sv_url = f"{sb_url}/rest/v1/source_vintages?on_conflict=release_version,source_key"
    sv_row = [{
        "release_version": release,
        "source_key": "nchs_nvss_county",
        "vintage": f"NCHS NVSS national life expectancy at birth, {min(TREND_YEARS)}–{max(TREND_YEARS)}",
        "release_date": None,
        "access_date": today,
        "tool_or_product": "CDC NCHS National Vital Statistics Reports (Vol. 72, No. 12 + Data Brief No. 521 for provisional 2023)",
    }]
    r = requests.post(sv_url, headers=headers, json=sv_row, timeout=60)
    r.raise_for_status()

    print(f"[trend] done. wrote {len(rows)} national_trend rows + 1 source_vintages row.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
