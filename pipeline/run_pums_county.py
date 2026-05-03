"""v1.0 — per-county lived FLY pipeline (Option B from methodology_v1_0.md).

Extends the v0.9 PUMS person-level scoring to PUMA aggregation, then maps
PUMA → county via the 2020 PUMA-county crosswalk to populate the
county_lived_fly Supabase table.

Per-person scoring is identical to v0.9 (run_pums.py / pums_thresholds.py).
v1.0 adds the loss-weighted formula on top of the existing tier reads:

    raw FLY    = 0.20 × #Strong + 0.10 × #Adequate          (v0.9, unchanged)
    lived FLY  = 0.50 + 0.10 × #Strong − 0.20 × #Below      (v1.0, new)

Per-PUMA aggregates are population-weighted (PWGTP) means across adults.
Per-county aggregates are tract-count-weighted across overlapping PUMAs
(see methodology_v1_0.md § PUMA proxy for county; build_county_to_puma.py
for the crosswalk).

Output: rows in public.county_lived_fly with columns
  release_version, geoid, lived_fly, raw_fly, n_strong, n_adequate,
  n_below, domains_resolved, puma_source, pums_sample_size, reliability_flag

Run after `pip install -r pipeline/requirements.txt`, with .env set
(CENSUS_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, RELEASE_VERSION),
from project root:
  python3 pipeline/run_pums_county.py 2024            # one year
  python3 pipeline/run_pums_county.py 2024 --dry-run  # no Supabase write
  python3 pipeline/run_pums_county.py 2024 --states ca,ny  # subset of states

The default year is the most-recent PUMS vintage (2024). v1.0 launch ships
with a single-year load; back-population of historical years is phase 5.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import requests

from lib import load_dotenv, pg_upsert
from pums_thresholds import (
    DOMAINS,
    score_education,
    score_family,
    score_health,
    score_purchasing_power,
    score_wealth,
)
from run_pums import (
    ALL_STATE_FIPS,
    FIPS_TO_POSTAL,
    HH_COLS,
    fetch_pums_state,
    parse_float,
    parse_int,
)

# v1.0 default to most-recent year. PUMS 2024 was released fall 2025.
DEFAULT_YEAR = 2024

# Reliability flag: PUMA with PUMS person count below this threshold is
# flagged on the map (65% opacity). Threshold is tunable per
# methodology_v1_0.md; 1500 ≈ 1.5% of a typical PUMA's ~100k residents.
RELIABILITY_THRESHOLD = 1500

# Per-person FLY constants — match pums_thresholds.person_fly_from_tiers
# but emit lived FLY simultaneously per the v1.0 formula.
DEVIATION_STRONG = 0.10   # above-Adequate gain
DEVIATION_BELOW = -0.10   # below-Adequate loss (× 2 weight applied below)
LIVED_BASELINE = 0.50     # 5 × 0.10 (the "all Adequate" reference point)
LOSS_WEIGHT = 2.0         # per Kahneman & Tversky 1992

# run_pums.py uses these but doesn't expose PUMA. Re-declare with PUMA added.
PERSON_COLS_WITH_PUMA = [
    "SERIALNO", "PWGTP", "AGEP", "SEX",
    "MAR", "SCHL", "ESR", "DIS", "ST", "PUMA",
]


def fetch_pums_state_with_puma(year: int, state_fips: str) -> list[dict]:
    """Variant of run_pums.fetch_pums_state that pulls PUMA in addition.

    fetch_pums_state in v0.9 hardcodes its own PERSON_COLS without PUMA.
    Rather than mutate that (which would leak PUMA into national_trend's
    record set without adding value), we redo the read here with PUMA
    included. The download is cached, so this is fast on second run.
    """
    # We use run_pums._fetch_zip indirectly via fetch_pums_state for
    # caching, but we have to re-parse the CSV ourselves to include PUMA.
    import csv as _csv
    import io as _io
    import zipfile as _zip

    from run_pums import _fetch_zip, _read_csv_from_zip  # type: ignore

    person_zip = _fetch_zip(year, state_fips, "p")
    hh_zip = _fetch_zip(year, state_fips, "h")
    persons: list[dict] = []
    with _zip.ZipFile(person_zip) as zf:
        csv_name = next(
            (n for n in zf.namelist()
             if n.lower().endswith(".csv") and n.lower().startswith("psam_")),
            None,
        )
        if csv_name is None:
            return []
        with zf.open(csv_name) as f:
            text = _io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            for row in _csv.DictReader(text):
                persons.append({k: row.get(k) for k in PERSON_COLS_WITH_PUMA})
    hh_rows = _read_csv_from_zip(hh_zip, HH_COLS)
    hh_by_sn = {h["SERIALNO"]: h for h in hh_rows if h.get("SERIALNO")}
    out = []
    for p in persons:
        h = hh_by_sn.get(p.get("SERIALNO"), {})
        merged = {**p, **{k: h.get(k) for k in ["HINCP", "NP", "VALP", "TEN"]}}
        out.append(merged)
    return out


def lived_fly_from_tiers(tiers: dict[str, str | None]) -> tuple[float, float, int, int, int, int]:
    """Compute (raw_fly, lived_fly, n_strong, n_adequate, n_below, n_resolved).

    A None tier (data unavailable for the domain) contributes nothing and
    isn't counted in the resolution. n_resolved < 5 means the person had
    one or more domains we couldn't score (rare under v0.9 + v1.0).
    """
    raw = 0.0
    deviation_sum = 0.0
    n_strong = n_adequate = n_below = n_resolved = 0
    for tier in tiers.values():
        if tier is None:
            continue
        n_resolved += 1
        if tier == "strong":
            raw += 0.20
            deviation_sum += DEVIATION_STRONG
            n_strong += 1
        elif tier == "adequate":
            raw += 0.10
            n_adequate += 1
        elif tier == "below":
            deviation_sum += DEVIATION_BELOW * LOSS_WEIGHT
            n_below += 1
    lived = LIVED_BASELINE + deviation_sum
    return raw, lived, n_strong, n_adequate, n_below, n_resolved


def aggregate_state_per_puma(records: list[dict], year: int, state_fips: str) -> dict[str, dict]:
    """Score a state's PUMS, return per-PUMA aggregates.

    Returns:
        { puma_5: {
            "weighted_pop":      float,         # all persons (incl. kids)
            "weighted_adults":   float,         # 18+ only
            "weighted_lived":    float,         # Σ w × person_lived (adult + inherited kid)
            "weighted_raw":      float,         # Σ w × person_raw
            "weighted_strong":   float,         # adult-only count
            "weighted_adequate": float,
            "weighted_below":    float,
            "n_records":         int,           # raw PUMS record count (sample size)
          }
        }

    PUMA is the level at which v1.0 county_lived_fly aggregates are built.
    Per-person scoring matches v0.9 run_pums.score_state_year exactly,
    including the children-inherit-HH-avg rule and group-quarters zeroing.
    """
    by_hh = defaultdict(list)
    for rec in records:
        sn = rec.get("SERIALNO")
        if sn:
            by_hh[sn].append(rec)

    per_puma: dict[str, dict] = defaultdict(lambda: {
        "weighted_pop": 0.0,
        "weighted_adults": 0.0,
        "weighted_lived": 0.0,
        "weighted_raw": 0.0,
        "weighted_strong": 0.0,
        "weighted_adequate": 0.0,
        "weighted_below": 0.0,
        "n_records": 0,
    })

    for sn, persons in by_hh.items():
        ages = [parse_int(p.get("AGEP")) for p in persons]
        n_adults = sum(1 for a in ages if a is not None and a >= 18)
        n_children = sum(1 for a in ages if a is not None and a < 18)

        hh = persons[0]
        hh_income = parse_float(hh.get("HINCP"))
        valp = parse_float(hh.get("VALP"))
        tenure = parse_int(hh.get("TEN"))
        pp_n_adults = n_adults or 1
        pp_tier = score_purchasing_power(hh_income, pp_n_adults, n_children, year)
        is_gq = "GQ" in sn

        # Score adults; collect (idx, weight, lived, raw, n_s, n_a, n_b).
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
            raw, lived, n_s, n_a, n_b, _ = lived_fly_from_tiers(tiers)
            adult_scores.append((idx, w, lived, raw, n_s, n_a, n_b))

        # HH avg adult lived/raw FLY (kids inherit; GQ kids → 0).
        adult_w_sum = sum(s[1] for s in adult_scores)
        if adult_w_sum > 0:
            hh_avg_lived = sum(s[1] * s[2] for s in adult_scores) / adult_w_sum
            hh_avg_raw = sum(s[1] * s[3] for s in adult_scores) / adult_w_sum
        else:
            hh_avg_lived = hh_avg_raw = 0.0

        adult_data_by_idx = {s[0]: s for s in adult_scores}

        for idx, p in enumerate(persons):
            w = parse_float(p.get("PWGTP"))
            if w is None or w <= 0:
                continue
            puma = p.get("PUMA")
            if not puma:
                continue
            puma = str(puma).zfill(5)  # left-pad to 5 digits to match PUMA20 format
            agg = per_puma[puma]
            agg["n_records"] += 1
            age = parse_int(p.get("AGEP"))
            agg["weighted_pop"] += w
            if age is not None and age >= 18 and idx in adult_data_by_idx:
                _, _, lived, raw, n_s, n_a, n_b = adult_data_by_idx[idx]
                agg["weighted_lived"] += w * lived
                agg["weighted_raw"] += w * raw
                agg["weighted_adults"] += w
                agg["weighted_strong"] += w * n_s
                agg["weighted_adequate"] += w * n_a
                agg["weighted_below"] += w * n_b
            else:
                # Child / unscored → inherit HH-avg adult.
                kid_lived = 0.0 if is_gq else hh_avg_lived
                kid_raw = 0.0 if is_gq else hh_avg_raw
                agg["weighted_lived"] += w * kid_lived
                agg["weighted_raw"] += w * kid_raw

    return per_puma


def aggregate_per_puma_all_states(year: int, states: list[str], verbose: bool = True) -> dict[tuple[str, str], dict]:
    """For each (state_fips, puma) return the v1.0 aggregate dict.

    Iterates the requested states. PUMA codes are unique only within state,
    so the output keys are (state_fips, puma_5).
    """
    nat: dict[tuple[str, str], dict] = {}
    for st in states:
        if verbose:
            print(f"[v1.0 county {year}] state {st}…", flush=True)
        try:
            records = fetch_pums_state_with_puma(year, st)
        except requests.HTTPError as e:
            print(f"[v1.0 county {year}] state {st} HTTP error: {e} — skipping", flush=True)
            continue
        if not records:
            print(f"[v1.0 county {year}] state {st}: no records", flush=True)
            continue
        per_puma = aggregate_state_per_puma(records, year, st)
        for puma, agg in per_puma.items():
            nat[(st, puma)] = agg
        if verbose:
            print(f"[v1.0 county {year}] state {st}: {len(per_puma)} PUMAs",
                  flush=True)
    return nat


def puma_aggregate_to_county_rows(
    per_puma: dict[tuple[str, str], dict],
    crosswalk: dict[str, list[dict]],
    release_version: str,
) -> list[dict]:
    """Blend PUMA aggregates into per-county lived FLY rows."""
    rows: list[dict] = []
    skipped_no_pumas = 0
    for geoid, parts in crosswalk.items():
        # Filter to PUMAs we actually have data for. If none match (because
        # we skipped that state in --states subset, or PUMS missed it),
        # emit no row for that county.
        usable = [
            (p, per_puma.get((p["state"], p["puma"])))
            for p in parts
        ]
        usable = [(p, agg) for p, agg in usable if agg is not None]
        if not usable:
            skipped_no_pumas += 1
            continue

        # Re-normalize weights across the usable subset (so a county whose
        # only-data-having PUMA had weight 0.95 still gets sensible numbers).
        weight_sum = sum(p["weight"] for p, _ in usable)
        if weight_sum <= 0:
            continue

        lived_num = raw_num = adults_num = pop_num = 0.0
        strong_num = adequate_num = below_num = 0.0
        sample_size = 0
        for p, agg in usable:
            w_norm = p["weight"] / weight_sum
            if agg["weighted_adults"] > 0:
                lived_num    += w_norm * (agg["weighted_lived"]    / agg["weighted_pop"])    * agg["weighted_pop"]
                raw_num      += w_norm * (agg["weighted_raw"]      / agg["weighted_pop"])    * agg["weighted_pop"]
                strong_num   += w_norm * (agg["weighted_strong"]   / agg["weighted_adults"]) * agg["weighted_adults"]
                adequate_num += w_norm * (agg["weighted_adequate"] / agg["weighted_adults"]) * agg["weighted_adults"]
                below_num    += w_norm * (agg["weighted_below"]    / agg["weighted_adults"]) * agg["weighted_adults"]
                adults_num   += w_norm * agg["weighted_adults"]
                pop_num      += w_norm * agg["weighted_pop"]
            sample_size += agg["n_records"]

        if pop_num <= 0 or adults_num <= 0:
            continue

        lived_fly = lived_num / pop_num
        raw_fly = raw_num / pop_num
        n_strong   = strong_num   / adults_num
        n_adequate = adequate_num / adults_num
        n_below    = below_num    / adults_num

        # Clamp lived_fly to schema bounds (CHECK: -0.500 to +1.000) — small
        # numerical overshoot at boundary cases is possible due to floating
        # point. raw_fly is similarly bounded.
        lived_fly = max(-0.500, min(1.000, lived_fly))
        raw_fly = max(0.000, min(1.000, raw_fly))

        # PUMA source string for the tooltip — single PUMA or composite.
        if len(usable) == 1:
            p = usable[0][0]
            puma_source = f"{p['state']}{p['puma']}"
        else:
            puma_source = "multi-PUMA pop-weighted"

        rows.append({
            "release_version": release_version,
            "geoid": geoid,
            "lived_fly": round(lived_fly, 3),
            "raw_fly": round(raw_fly, 3),
            "n_strong": round(n_strong, 2),
            "n_adequate": round(n_adequate, 2),
            "n_below": round(n_below, 2),
            "domains_resolved": 5,  # all PUMS-scored adults have all 5 by design
            "puma_source": puma_source,
            "pums_sample_size": sample_size,
            "reliability_flag": sample_size < RELIABILITY_THRESHOLD,
        })
    if skipped_no_pumas:
        print(f"[v1.0 county] {skipped_no_pumas} counties skipped "
              f"(no PUMS data for any overlapping PUMA)", flush=True)
    return rows


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    release = os.environ.get("RELEASE_VERSION", "v2025.12")

    # CLI: `python3 run_pums_county.py [year] [--states ca,ny] [--dry-run]`
    args = sys.argv[1:]
    year = DEFAULT_YEAR
    state_subset: list[str] | None = None
    dry_run = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--states":
            i += 1
            postal = [s.strip().lower() for s in args[i].split(",")]
            postal_to_fips = {v: k for k, v in FIPS_TO_POSTAL.items()}
            state_subset = [postal_to_fips[p] for p in postal if p in postal_to_fips]
        else:
            try:
                year = int(a)
            except ValueError:
                pass
        i += 1

    states = state_subset or ALL_STATE_FIPS
    print(f"[v1.0 county] year={year} release={release} states={len(states)} "
          f"dry_run={dry_run}", flush=True)

    crosswalk_path = os.path.join(here, "county_to_puma.json")
    if not os.path.exists(crosswalk_path):
        raise SystemExit(f"missing {crosswalk_path}; run "
                         f"`python3 pipeline/build_county_to_puma.py` first")
    with open(crosswalk_path) as f:
        crosswalk = json.load(f)

    per_puma = aggregate_per_puma_all_states(year, states)
    if not per_puma:
        print("[v1.0 county] no PUMA aggregates produced; aborting.")
        return 1

    rows = puma_aggregate_to_county_rows(per_puma, crosswalk, release)
    print(f"\n[v1.0 county] produced {len(rows):,} county rows")
    if rows:
        flagged = sum(1 for r in rows if r["reliability_flag"])
        neg = sum(1 for r in rows if r["lived_fly"] < 0)
        print(f"[v1.0 county]   reliability-flagged: {flagged:,}")
        print(f"[v1.0 county]   negative lived_fly:  {neg:,}")
        sample = rows[:5]
        print(f"\n=== SAMPLE ROWS (first 5) ===")
        print(json.dumps(sample, indent=2))

    if dry_run:
        print("\n[v1.0 county] --dry-run set; not writing to Supabase.")
        return 0

    if not (sb_url and sb_key):
        print("\n[v1.0 county] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing; "
              "skipping write.")
        return 1

    pg_upsert(
        sb_url, sb_key, "county_lived_fly", rows,
        on_conflict="release_version,geoid",
    )
    print(f"\n[v1.0 county] upserted {len(rows):,} rows into county_lived_fly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
