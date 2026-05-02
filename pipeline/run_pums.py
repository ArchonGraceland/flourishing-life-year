"""v0.8 — PUMS-based person-level FLY pipeline.

Replaces the geomean-of-national-means approach in run_trend.py. Every PUMS
person record is scored Strong / Adequate / Below across 5 domains using the
2019-frozen absolute thresholds in pums_thresholds.py. Each person's FLY is
0.20 × #Strong + 0.10 × #Adequate (range 0.0–1.0). National total FLY is the
PWGTP-weighted sum.

Years: 2019, 2021, 2022, 2023. Census did not release a 2020 1-year PUMS due
to COVID-era response-quality concerns; the trend chart shows a 2020 gap.

Source: per-state PUMS person-record CSV ZIPs from the Census Bureau bulk
endpoint (the public PUMS *API* throws 500s for non-trivial queries; the
bulk CSVs are the documented stable distribution channel).

  https://www2.census.gov/programs-surveys/acs/data/pums/{year}/1-Year/csv_p{st}.zip

ZIP contains psam_p{st}.csv with columns: SERIALNO, PWGTP, AGEP, SEX, HINCP,
NP, VALP, TEN, MAR, SCHL, ESR, DIS, ST, PUMA, plus replicate weights.

Writes to Supabase national_trend (release_version, year, total_fly,
composite_index, per_domain_breakdown). composite_index is total_fly[T] /
total_fly[2019] × 100, preserving the §balance chart's 2019 = 100 anchor.

Run after `pip install -r pipeline/requirements.txt` from project root:
  python3 pipeline/run_pums.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict
from datetime import date

import requests

from lib import EXCLUDED_STATE_FIPS, load_dotenv, pg_upsert
from pums_thresholds import (
    DOMAINS,
    person_fly_from_tiers,
    score_education,
    score_family,
    score_health,
    score_purchasing_power,
    score_wealth,
)

YEARS = [2019, 2021, 2022, 2023, 2024]  # 2020 omitted: no 1-year PUMS released

# Person-record columns we need (psam_p{st}.csv).
PERSON_COLS = [
    "SERIALNO", "PWGTP", "AGEP", "SEX",
    "MAR", "SCHL", "ESR", "DIS", "ST",
]
# Household-record columns we need (psam_h{st}.csv) — joined to persons by SERIALNO.
HH_COLS = ["SERIALNO", "HINCP", "NP", "VALP", "TEN"]

# State FIPS → 2-letter postal abbreviation (lowercase, used in PUMS filenames).
FIPS_TO_POSTAL = {
    "01": "al", "02": "ak", "04": "az", "05": "ar", "06": "ca",
    "08": "co", "09": "ct", "10": "de", "11": "dc", "12": "fl",
    "13": "ga", "15": "hi", "16": "id", "17": "il", "18": "in",
    "19": "ia", "20": "ks", "21": "ky", "22": "la", "23": "me",
    "24": "md", "25": "ma", "26": "mi", "27": "mn", "28": "ms",
    "29": "mo", "30": "mt", "31": "ne", "32": "nv", "33": "nh",
    "34": "nj", "35": "nm", "36": "ny", "37": "nc", "38": "nd",
    "39": "oh", "40": "ok", "41": "or", "42": "pa", "44": "ri",
    "45": "sc", "46": "sd", "47": "tn", "48": "tx", "49": "ut",
    "50": "vt", "51": "va", "53": "wa", "54": "wv", "55": "wi",
    "56": "wy",
}
ALL_STATE_FIPS = sorted(FIPS_TO_POSTAL.keys())

PUMS_BULK_TPL = (
    "https://www2.census.gov/programs-surveys/acs/data/pums/{year}/1-Year/"
    "csv_{kind}{st}.zip"
)
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".pums_cache")


def _fetch_zip(year: int, state_fips: str, kind: str) -> str:
    """Download a per-state PUMS zip ('p' = person, 'h' = household), cache, return path."""
    st = FIPS_TO_POSTAL[state_fips]
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{year}_{kind}_{st}.zip")
    if not os.path.exists(cache_path):
        url = PUMS_BULK_TPL.format(year=year, st=st, kind=kind)
        r = requests.get(url, stream=True, timeout=600)
        r.raise_for_status()
        with open(cache_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return cache_path


def _read_csv_from_zip(zip_path: str, needed_cols: list[str]) -> list[dict]:
    out: list[dict] = []
    with zipfile.ZipFile(zip_path) as zf:
        csv_name = next(
            (n for n in zf.namelist()
             if n.lower().endswith(".csv") and n.lower().startswith("psam_")),
            None,
        )
        if csv_name is None:
            csv_name = next(
                (n for n in zf.namelist() if n.lower().endswith(".csv")),
                None,
            )
        if csv_name is None:
            return []
        with zf.open(csv_name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            reader = csv.DictReader(text)
            for row in reader:
                out.append({k: row.get(k) for k in needed_cols})
    return out


def fetch_pums_state(year: int, state_fips: str, _api_key_unused: str = "") -> list[dict]:
    """Download per-state PUMS person + household CSVs, join by SERIALNO, return persons.

    Cache lives in pipeline/.pums_cache/{year}_{p,h}_{st}.zip — gitignored.
    Person + household ZIPs together are ~2-100MB per state. After first fetch,
    runs are fast (parse only).
    """
    person_zip = _fetch_zip(year, state_fips, "p")
    hh_zip = _fetch_zip(year, state_fips, "h")
    persons = _read_csv_from_zip(person_zip, PERSON_COLS)
    hh_rows = _read_csv_from_zip(hh_zip, HH_COLS)
    hh_by_sn = {h["SERIALNO"]: h for h in hh_rows if h.get("SERIALNO")}
    out = []
    for p in persons:
        h = hh_by_sn.get(p.get("SERIALNO"), {})
        merged = {**p, **{k: h.get(k) for k in ["HINCP", "NP", "VALP", "TEN"]}}
        out.append(merged)
    return out


def parse_int(v):
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def parse_float(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def score_state_year(records: list[dict], year: int, state_fips: str) -> dict:
    """Run all 5 domain scores over PUMS records, return weighted aggregates.

    v0.9 children-inherit-HH-average rule:
      - Adults (age 18+) are scored normally on the 5 domains; per-domain
        breakdowns are computed from adults only.
      - Children inherit the weighted-mean person_FLY of the adults in
        their household. They do NOT get individual tier ratings, so
        domain shares stay adult-only.
      - Group-quarters children (foster care, juvenile detention,
        institutional placement) score 0 — institutional placement is
        a family-domain failure by the framework's own logic.
      - HHs with kids and no adults: kids score 0.

    Returns:
        {
          "weighted_pop": float,             # everyone (incl. kids)
          "weighted_adults": float,          # 18+ only — denominator for breakdown
          "weighted_fly": float,             # adult FLY + inherited kid FLY
          "domain_strong":   {d: weight},    # adult-only
          "domain_adequate": {d: weight},    # adult-only
          "domain_below":    {d: weight},    # adult-only
        }
    """
    by_hh = defaultdict(list)
    for rec in records:
        sn = rec.get("SERIALNO")
        if not sn:
            continue
        by_hh[sn].append(rec)

    out = {
        "weighted_pop": 0.0,
        "weighted_adults": 0.0,
        "weighted_fly": 0.0,
        "domain_strong": defaultdict(float),
        "domain_adequate": defaultdict(float),
        "domain_below": defaultdict(float),
    }

    for sn, persons in by_hh.items():
        ages = [parse_int(p.get("AGEP")) for p in persons]
        n_adults_in_hh = sum(1 for a in ages if a is not None and a >= 18)
        n_children_in_hh = sum(1 for a in ages if a is not None and a < 18)

        hh = persons[0]
        hh_income = parse_float(hh.get("HINCP"))
        valp = parse_float(hh.get("VALP"))
        tenure = parse_int(hh.get("TEN"))
        # PP threshold uses HH composition. If a HH has 0 adults (rare;
        # kid-headed HH), treat HH head as adult for the SPM equivalence calc.
        pp_n_adults = n_adults_in_hh or 1
        pp_tier = score_purchasing_power(hh_income, pp_n_adults, n_children_in_hh, year)
        is_gq = "GQ" in sn  # group-quarters serial numbers contain "GQ"

        # Pass 1: score adults, collect (idx, weight, person_fly, tiers).
        adult_scores = []
        for idx, p in enumerate(persons):
            w = parse_float(p.get("PWGTP"))
            if w is None or w <= 0:
                continue
            age = parse_int(p.get("AGEP"))
            if age is None or age < 18:
                continue
            tiers = {
                "purchasing_power": pp_tier,
                "wealth": score_wealth(tenure, valp, age, year),
                "family": score_family(
                    age, parse_int(p.get("MAR")), parse_int(hh.get("NP"))),
                "health": score_health(
                    state_fips, parse_int(p.get("DIS")) == 1),
                "education": score_education(
                    parse_int(p.get("SCHL")), parse_int(p.get("ESR")), age),
            }
            person_fly = person_fly_from_tiers(tiers)
            adult_scores.append((idx, w, person_fly, tiers))

        # HH average adult FLY, weighted by PWGTP. 0 if no adults / GQ kids.
        adult_w_sum = sum(s[1] for s in adult_scores)
        hh_avg_adult_fly = (
            sum(s[1] * s[2] for s in adult_scores) / adult_w_sum
            if adult_w_sum > 0 else 0.0
        )

        # Pass 2: aggregate.
        adult_idx_set = {s[0] for s in adult_scores}
        adult_data_by_idx = {s[0]: s for s in adult_scores}
        for idx, p in enumerate(persons):
            w = parse_float(p.get("PWGTP"))
            if w is None or w <= 0:
                continue
            age = parse_int(p.get("AGEP"))
            out["weighted_pop"] += w
            if age is not None and age >= 18 and idx in adult_idx_set:
                _, _, person_fly, tiers = adult_data_by_idx[idx]
                out["weighted_fly"] += w * person_fly
                out["weighted_adults"] += w
                for d, t in tiers.items():
                    if t == "strong":
                        out["domain_strong"][d] += w
                    elif t == "adequate":
                        out["domain_adequate"][d] += w
                    else:
                        out["domain_below"][d] += w
            else:
                # Child (or unscored adult, e.g., missing age) — inherit HH avg.
                # GQ kids → 0 (institutional placement = family-domain failure).
                kid_fly = 0.0 if is_gq else hh_avg_adult_fly
                out["weighted_fly"] += w * kid_fly

    return out


def aggregate_year(year: int, api_key: str, verbose: bool = True) -> dict:
    """Run all states for a given year, return national aggregates."""
    nat = {
        "weighted_pop": 0.0,
        "weighted_fly": 0.0,
        "weighted_adults": 0.0,
        "domain_strong": defaultdict(float),
        "domain_adequate": defaultdict(float),
        "domain_below": defaultdict(float),
    }
    for st in ALL_STATE_FIPS:
        if verbose:
            print(f"[pums {year}] state {st}…", flush=True)
        try:
            records = fetch_pums_state(year, st, api_key)
        except requests.HTTPError as e:
            print(f"[pums {year}] state {st} HTTP error: {e} — skipping", flush=True)
            continue
        if not records:
            print(f"[pums {year}] state {st}: no records", flush=True)
            continue
        st_agg = score_state_year(records, year, st)
        nat["weighted_pop"] += st_agg["weighted_pop"]
        nat["weighted_fly"] += st_agg["weighted_fly"]
        nat["weighted_adults"] += st_agg["weighted_adults"]
        for d in DOMAINS:
            nat["domain_strong"][d] += st_agg["domain_strong"].get(d, 0.0)
            nat["domain_adequate"][d] += st_agg["domain_adequate"].get(d, 0.0)
            nat["domain_below"][d] += st_agg["domain_below"].get(d, 0.0)
        if verbose:
            print(f"[pums {year}] state {st}: pop={st_agg['weighted_pop']:,.0f} "
                  f"fly={st_agg['weighted_fly']:,.0f}", flush=True)
    return nat


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    api_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    release = os.environ.get("RELEASE_VERSION", "v2025.12")

    # Allow YEARS override via CLI: `python3 run_pums.py 2019` or `2019,2023`.
    years = YEARS
    if len(sys.argv) > 1:
        years = [int(y) for y in sys.argv[1].split(",")]

    by_year = {}
    for year in years:
        agg = aggregate_year(year, api_key)
        total_fly = round(agg["weighted_fly"])
        # Per-capita FLY = total FLY (incl. inherited kid contributions) ÷ full pop.
        avg_fly = agg["weighted_fly"] / agg["weighted_pop"] if agg["weighted_pop"] else None
        breakdown = {}
        adult_pop = agg["weighted_adults"] or 1.0
        for d in DOMAINS:
            breakdown[d] = {
                "strong_share": round(agg["domain_strong"][d] / adult_pop, 4),
                "adequate_share": round(agg["domain_adequate"][d] / adult_pop, 4),
                "below_share": round(agg["domain_below"][d] / adult_pop, 4),
            }
        by_year[year] = {
            "year": year,
            "total_fly": total_fly,
            "weighted_pop": round(agg["weighted_pop"]),
            "weighted_adults": round(agg["weighted_adults"]),
            "avg_person_fly": round(avg_fly, 4) if avg_fly is not None else None,
            "per_domain_breakdown": breakdown,
        }
        print(f"\n[pums {year}] TOTAL FLY = {total_fly:,}  "
              f"avg_person_fly = {avg_fly:.4f}  "
              f"pop = {agg['weighted_pop']:,.0f}  "
              f"adults = {agg['weighted_adults']:,.0f}\n", flush=True)

    # Compute composite_index = total_fly[T] / total_fly[2019] × 100.
    fly_2019 = by_year.get(2019, {}).get("total_fly")
    rows = []
    for year, rec in sorted(by_year.items()):
        composite_index = (
            round(rec["total_fly"] / fly_2019 * 100, 2)
            if fly_2019 else None
        )
        rows.append({
            "release_version": release,
            "year": year,
            "composite_index": composite_index,
            "per_domain_breakdown": rec["per_domain_breakdown"],
            "total_fly": rec["total_fly"],
        })

    # Print full result for inspection before writing.
    print("\n=== ROWS TO UPSERT ===")
    print(json.dumps(rows, indent=2, default=str))

    if "--dry-run" in sys.argv:
        print("\n[pums] --dry-run set; not writing to Supabase.")
        return 0

    if not (sb_url and sb_key):
        print("\n[pums] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing; skipping write.")
        return 1

    pg_upsert(
        sb_url, sb_key, "national_trend", rows,
        on_conflict="release_version,year",
    )
    print(f"\n[pums] upserted {len(rows)} rows into national_trend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
