"""Build national_trends.json — annual national-level values for each of
the five FLY domain headline indicators, back to 2010.

Answers: "is the nation moving forward?" The county FLY index is relative
within a release (re-percentile-ranked each year), so it can't speak to
direction over time. Absolute national levels can — and the reader compares
trajectories themselves.

Pulls ACS 1-yr at us:1 for income, homeownership, family, education. ACS 1-yr
was paused for 2020 (COVID); use ACS 5-yr 2016-2020 for that year. CPI and
life expectancy are hardcoded from canonical published series.

Re-run when new ACS vintages drop.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request

from lib import load_dotenv  # type: ignore

# BLS CPI-U annual averages, series CUUR0000SA0. Stable historical data.
CPI_BY_YEAR = {
    2010: 218.056, 2011: 224.939, 2012: 229.594, 2013: 232.957, 2014: 236.736,
    2015: 237.017, 2016: 240.007, 2017: 245.120, 2018: 251.107, 2019: 255.657,
    2020: 258.811, 2021: 270.970, 2022: 292.655, 2023: 304.702, 2024: 313.689,
}
CPI_BASE_YEAR = 2024  # deflate to constant 2024 dollars (latest full year)
CPI_BASE = CPI_BY_YEAR[CPI_BASE_YEAR]

# NCHS NVSS US life expectancy at birth (both sexes, all races). Canonical
# values from CDC/NCHS annual reports; 2023 is provisional (NCHS Data Brief
# No. 521, Dec 2024). 2020-2022 reflect the COVID disruption.
LIFE_EXPECTANCY_BY_YEAR = {
    2010: 78.7, 2011: 78.7, 2012: 78.8, 2013: 78.8, 2014: 78.9,
    2015: 78.7, 2016: 78.6, 2017: 78.6, 2018: 78.7, 2019: 78.8,
    2020: 77.0, 2021: 76.4, 2022: 77.5, 2023: 78.4,
}

YEARS = list(range(2010, 2025))      # 2010..2024 inclusive
USE_5YR_FOR = {2020}                  # ACS 1-yr 2020 not released

ACS_TPL = "https://api.census.gov/data/{year}/acs/{flavor}?get={vars}&for=us:1&key={key}"


def fetch_acs_us(year: int, var_codes: list[str], key: str) -> dict[str, float | None]:
    flavor = "acs5" if year in USE_5YR_FOR else "acs1"
    url = ACS_TPL.format(year=year, flavor=flavor,
                         vars=",".join(var_codes), key=key)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    header, row = data[0], data[1]
    out: dict[str, float | None] = {}
    for h, v in zip(header, row):
        if h in var_codes:
            try:
                out[h] = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                out[h] = None
    return out


def build_purchasing_power(key: str) -> dict[int, float]:
    out = {}
    for year in YEARS:
        try:
            r = fetch_acs_us(year, ["B19013_001E"], key)
            nominal = r.get("B19013_001E")
            if nominal is None:
                continue
            real = nominal / (CPI_BY_YEAR[year] / CPI_BASE)
            out[year] = round(real, 0)
        except Exception as e:
            print(f"  [pp] {year} failed: {e}", file=sys.stderr)
    return out


def build_wealth_homeownership(key: str) -> dict[int, float]:
    out = {}
    for year in YEARS:
        try:
            r = fetch_acs_us(year, ["B25003_001E", "B25003_002E"], key)
            tot, own = r.get("B25003_001E"), r.get("B25003_002E")
            if tot and tot > 0 and own is not None:
                out[year] = round(own / tot, 4)
        except Exception as e:
            print(f"  [wealth] {year} failed: {e}", file=sys.stderr)
    return out


def build_family_married(key: str) -> dict[int, float]:
    """Married-couple share of children under 18.

    Pre-2019 schema: B09005_003E (in family households: married-couple family).
    Post-2019 schema: B09005_002E (married-couple household).

    Cohabiting couples are NOT included so the series is comparable across
    the 2019 schema break — the page caption notes this.
    """
    out = {}
    for year in YEARS:
        var = "B09005_002E" if year >= 2019 else "B09005_003E"
        try:
            r = fetch_acs_us(year, ["B09005_001E", var], key)
            tot, marr = r.get("B09005_001E"), r.get(var)
            if tot and tot > 0 and marr is not None:
                out[year] = round(marr / tot, 4)
        except Exception as e:
            print(f"  [family] {year} failed: {e}", file=sys.stderr)
    return out


def build_education_associate_plus(key: str) -> dict[int, float]:
    """Share of pop 25+ with associate's degree or higher (B15003 _021..._025)."""
    out = {}
    associate_plus = ["B15003_021E", "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E"]
    for year in YEARS:
        try:
            r = fetch_acs_us(year, ["B15003_001E"] + associate_plus, key)
            tot = r.get("B15003_001E")
            if tot and tot > 0 and all(r.get(v) is not None for v in associate_plus):
                num = sum(r[v] for v in associate_plus)
                out[year] = round(num / tot, 4)
        except Exception as e:
            print(f"  [edu] {year} failed: {e}", file=sys.stderr)
    return out


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    key = os.environ["CENSUS_API_KEY"]

    print("[trends] purchasing power…", file=sys.stderr)
    pp = build_purchasing_power(key)
    print("[trends] wealth (homeownership)…", file=sys.stderr)
    wealth = build_wealth_homeownership(key)
    print("[trends] family (married-couple share)…", file=sys.stderr)
    family = build_family_married(key)
    print("[trends] education (associate+)…", file=sys.stderr)
    edu = build_education_associate_plus(key)

    out = {
        "release_version": "v2025.12 — national trends companion",
        "base_year_dollars": CPI_BASE_YEAR,
        "indicators": {
            "purchasing_power": {
                "label": "Real median household income",
                "unit": f"constant {CPI_BASE_YEAR} dollars",
                "format": "currency",
                "values": pp,
            },
            "wealth": {
                "label": "Homeownership rate",
                "unit": "share of occupied housing units",
                "format": "percent",
                "values": wealth,
            },
            "family": {
                "label": "Children under 18 in married-couple families",
                "unit": "share of children under 18",
                "format": "percent",
                "note": "Pre-2019 schema; cohabiting couples not separately reported. Post-2019 the ACS broke out cohabiting two-parent households; this series stays married-only for cross-year comparability.",
                "values": family,
            },
            "health": {
                "label": "Life expectancy at birth",
                "unit": "years",
                "format": "years",
                "note": "CDC NCHS National Vital Statistics; 2020–2022 reflect COVID-era mortality; 2023 is provisional (NCHS Data Brief No. 521, Dec 2024).",
                "values": LIFE_EXPECTANCY_BY_YEAR,
            },
            "education": {
                "label": "Adults 25+ with associate's degree or higher",
                "unit": "share of adults 25+",
                "format": "percent",
                "values": edu,
            },
        },
        "sources": {
            "acs": "U.S. Census Bureau ACS 1-yr (5-yr 2016-2020 used for 2020 since 1-yr was not released)",
            "cpi": "BLS CPI-U all-items, annual averages, series CUUR0000SA0",
            "nchs": "CDC National Center for Health Statistics, National Vital Statistics annual reports",
        },
    }
    out_path = os.path.join(here, "..", "national_trends.json")
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"), default=str)
    print(f"[trends] wrote {os.path.realpath(out_path)} "
          f"({os.path.getsize(out_path):,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
