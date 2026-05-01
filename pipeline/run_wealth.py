"""Wealth domain — geometric mean of FOUR proxies (Q2 + Q8.5):
  1. ACS B25003 — homeownership rate
  2. ACS B25077 — median home value (owner-occupied)
  3. IRS SOI county — investment income share = (A00600 + A01000) / A02650
       i.e. (ordinary dividends + net capital gain) / AGI
  4. IRS SOI county — retirement-savings flow = (N03210 + N03150) / N1
       i.e. share of returns claiming an IRA deduction or self-employed retirement plan
       contribution. Q8.5 amendment (2026-05-01): Q8.1.3 requires both savings *capacity*
       (proxies 1–3, all stocks) AND active *practice* (this flow proxy). 401(k) / 403(b)
       contributions don't appear on the tax return — they're netted out of wages before
       AGI — so this proxy undercounts workplace-plan savers; it's directional, not
       comprehensive. Documented as such on the methodology page.

Q6 (Q8.5 update): needs ≥ 3 of 4 components. With 3, geometric mean of available;
with ≤ 2, suppressed. (Was ≥ 2 of 3 before Q8.5.)

The wealth domain is explicitly a *proxy stack* (no federal county-level wealth survey
exists), and the methodology page must say so. Each component is percentile-ranked
separately; the domain score is the geometric mean of the available percentile ranks.
"""

from __future__ import annotations

import csv
import io
import math
import os
import sys
from datetime import date

import requests

from lib import EXCLUDED_STATE_FIPS, load_dotenv, parse_acs_value, pg_delete_domain, pg_upsert, upload_raw
from percentile import percentile_rank

DOMAIN = "wealth"
ACS_VINTAGE_YEAR = 2024

# B25003 — Tenure (universe: occupied housing units).
# _001 total | _002 owner-occupied | _003 renter-occupied
B25003_VARS = ["B25003_001E", "B25003_001M", "B25003_002E", "B25003_002M"]

# B25077 — Median value, owner-occupied housing units (single estimate).
B25077_VARS = ["B25077_001E", "B25077_001M"]

ACS_VARS = B25003_VARS + B25077_VARS

# IRS SOI county-level individual income tax data, TY2022 (released Feb 2025).
SOI_URL = "https://www.irs.gov/pub/irs-soi/22incyallagi.csv"
SOI_TY = "TY2022"
SOI_RELEASED = "2025-02-01"  # approximate; refined per release notes if needed


def fetch_acs(api_key: str) -> list[dict]:
    url = f"https://api.census.gov/data/{ACS_VINTAGE_YEAR}/acs/acs5"
    params = {"get": "NAME," + ",".join(ACS_VARS), "for": "county:*", "key": api_key}
    r = requests.get(url, params=params, timeout=180)
    r.raise_for_status()
    rows = r.json()
    header, *data = rows
    out = []
    for row in data:
        rec = dict(zip(header, row))
        if rec["state"] in EXCLUDED_STATE_FIPS:
            continue
        rec["geoid"] = rec["state"] + rec["county"]
        for v in ACS_VARS:
            rec[v] = parse_acs_value(rec[v])
        out.append(rec)
    return out


def fetch_irs_soi() -> tuple[bytes, dict[str, dict]]:
    """Download SOI county CSV and aggregate per-county sums across all AGI brackets.

    Returns (raw_csv_bytes, {geoid: {agi, div, cap, n1, n_ira, n_seretire}}).
    n_ira = N03210 (IRA-deduction returns); n_seretire = N03150 (self-employed
    retirement plan returns). Sum / N1 = retirement-savings-flow proxy (Q8.5).
    Some columns may be missing in older or partial extracts; treated as 0.
    """
    r = requests.get(SOI_URL, timeout=180)
    r.raise_for_status()
    blob = r.content
    text = blob.decode("cp1252")  # IRS SOI county file uses CP-1252 (Spanish-language county names)
    reader = csv.DictReader(io.StringIO(text))
    agg: dict[str, dict[str, float]] = {}
    for row in reader:
        sf = row["STATEFIPS"].strip().zfill(2)
        cf = row["COUNTYFIPS"].strip().zfill(3)
        if sf in EXCLUDED_STATE_FIPS:
            continue
        if cf == "000":  # state-total rollup row, skip
            continue
        geoid = sf + cf
        a = agg.setdefault(geoid, {
            "agi": 0.0, "div": 0.0, "cap": 0.0, "n1": 0.0,
            "n_ira": 0.0, "n_seretire": 0.0,
        })
        try:
            a["agi"] += float(row.get("A02650") or 0)
            a["div"] += float(row.get("A00600") or 0)
            a["cap"] += float(row.get("A01000") or 0)
            a["n1"]  += float(row.get("N1") or 0)
            # Q8.5: IRA deductions and self-employed retirement contributions.
            # Either column may be absent in some SOI vintages — default to 0.
            a["n_ira"]      += float(row.get("N03210") or 0)
            a["n_seretire"] += float(row.get("N03150") or 0)
        except ValueError:
            pass
    return blob, agg


def proportion_with_moe(num, num_moe, denom, denom_moe):
    if denom is None or denom <= 0 or num is None or num < 0:
        return None, None
    p = num / denom
    nm = num_moe or 0
    dm = denom_moe or 0
    radicand = nm ** 2 - (p ** 2) * (dm ** 2)
    moe = (math.sqrt(radicand) if radicand >= 0
           else math.sqrt(nm ** 2 + (p ** 2) * (dm ** 2))) / denom
    return p, moe


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]
    today = date.today().isoformat()

    print(f"[wealth] release={release} fetching ACS B25003 + B25077…")
    acs = fetch_acs(census_key)
    print(f"[wealth] {len(acs)} county rows")

    print("[wealth] fetching IRS SOI county aggregate (TY2022)…")
    soi_blob, soi = fetch_irs_soi()
    print(f"[wealth] {len(soi)} SOI county aggregates")

    c1: dict[str, float] = {}      # homeownership rate
    c2: dict[str, float] = {}      # median home value
    c3: dict[str, float] = {}      # investment income share
    c4: dict[str, float] = {}      # retirement-savings flow share (Q8.5)
    c1_moe: dict[str, float] = {}  # homeownership-rate MOE/estimate
    c2_moe: dict[str, float] = {}  # median home-value MOE/estimate
    suppression_by_county: dict[str, list[tuple[str, str]]] = {}

    def mark(geoid: str, source: str, reason: str) -> None:
        suppression_by_county.setdefault(geoid, []).append((source, reason))

    for rec in acs:
        geoid = rec["geoid"]
        # Component 1: homeownership rate (Q1 amendment: high MOE no longer suppresses)
        own_rate, own_moe = proportion_with_moe(
            rec["B25003_002E"], rec["B25003_002M"],
            rec["B25003_001E"], rec["B25003_001M"])
        if own_rate is None:
            mark(geoid, "acs_b25003", "acs_missing")
        else:
            c1[geoid] = own_rate
            if own_rate > 0 and own_moe is not None:
                c1_moe[geoid] = own_moe / own_rate

        # Component 2: median home value
        mhv = rec["B25077_001E"]
        mhv_moe = rec["B25077_001M"]
        if mhv is None or mhv <= 0:
            mark(geoid, "acs_b25077", "acs_missing")
        else:
            c2[geoid] = float(mhv)
            if mhv > 0 and mhv_moe is not None:
                c2_moe[geoid] = mhv_moe / mhv

        # Component 3: investment income share (IRS SOI; source-default suppression — no MOE)
        s = soi.get(geoid)
        if s is None or s["agi"] <= 0:
            mark(geoid, "irs_soi", "soi_unavailable_or_negative_agi")
        else:
            c3[geoid] = (s["div"] + s["cap"]) / s["agi"]

        # Component 4 (Q8.5): retirement-savings flow = (IRA + self-employed retirement
        # returns) / total returns. IRS SOI; source-default suppression — no MOE.
        # Suppress component-only (NOT the whole row) when N1 is missing or zero;
        # the wealth domain's Q6 ≥3-of-4 rule handles partial-component cases.
        if s is None or not s.get("n1") or s["n1"] <= 0:
            mark(geoid, "irs_soi_retirement", "soi_unavailable")
        else:
            c4[geoid] = (s.get("n_ira", 0) + s.get("n_seretire", 0)) / s["n1"]

    print(f"[wealth] usable c1={len(c1)} c2={len(c2)} c3={len(c3)} c4={len(c4)} "
          f"(c1 MOE>30% flagged={sum(1 for m in c1_moe.values() if m > 0.30)}, "
          f"c2 MOE>30% flagged={sum(1 for m in c2_moe.values() if m > 0.30)})")

    r1 = percentile_rank(c1)
    r2 = percentile_rank(c2)
    r3 = percentile_rank(c3)
    r4 = percentile_rank(c4)

    domain_rows = []
    suppressed: list[dict] = []
    geoids = {r["geoid"] for r in acs}
    domain_flagged = 0
    for geoid in geoids:
        present = []
        if geoid in r1: present.append(("c1", r1[geoid], c1_moe.get(geoid)))
        if geoid in r2: present.append(("c2", r2[geoid], c2_moe.get(geoid)))
        if geoid in r3: present.append(("c3", r3[geoid], None))  # IRS SOI: no MOE
        if geoid in r4: present.append(("c4", r4[geoid], None))  # IRS SOI: no MOE
        if len(present) >= 3:  # Q6 (Q8.5 update): need ≥ 3 of 4
            ranks = [p[1] for p in present]
            geo = math.exp(sum(math.log(x) for x in ranks) / len(ranks))
            # Domain reliability: max MOE across the ACS components actually used.
            mp_vals = [p[2] for p in present if p[2] is not None]
            mp = max(mp_vals) if mp_vals else None
            if mp and mp > 0.30:
                domain_flagged += 1
            domain_rows.append({
                "release_version": release, "geoid": geoid, "domain": DOMAIN,
                "percentile": max(1, min(100, round(geo))),
                "raw_value": None,
                "moe_pct": mp,
            })
        else:
            # < 3 components -> domain suppressed. Record one flag with the most informative reason.
            failures = suppression_by_county.get(geoid, [])
            reason = "wealth_lt_3_proxies" if failures else "wealth_no_data"
            source = failures[0][0] if failures else None
            suppressed.append({
                "release_version": release, "geoid": geoid, "domain": DOMAIN,
                "reason": reason, "source_key": source,
            })

    print(f"[wealth] domain_scores={len(domain_rows)} (MOE>30% flagged={domain_flagged}) "
          f"suppressed={len(suppressed)}")

    print("[wealth] uploading raw extracts…")
    buf = io.StringIO()
    fields = ["geoid", "NAME"] + ACS_VARS
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for rec in acs:
        w.writerow(rec)
    body = buf.getvalue().encode()
    upload_raw(sb_url, sb_key, release, "acs_b25003", "B25003.csv", body)
    upload_raw(sb_url, sb_key, release, "acs_b25077", "B25077.csv", body)
    upload_raw(sb_url, sb_key, release, "irs_soi", "22incyallagi.csv", soi_blob)

    print("[wealth] clearing prior rows for this release+domain…")
    pg_delete_domain(sb_url, sb_key, "domain_scores", release, DOMAIN)
    pg_delete_domain(sb_url, sb_key, "suppression_flags", release, DOMAIN)

    print("[wealth] writing release / source_vintages / domain_scores / suppression_flags…")
    pg_upsert(sb_url, sb_key, "releases",
              [{"version": release, "released_at": today, "notes": "dev run"}],
              on_conflict="version")
    pg_upsert(sb_url, sb_key, "source_vintages", [
        {"release_version": release, "source_key": "acs_b25003",
         "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
         "release_date": None, "access_date": today,
         "tool_or_product": "Census Bureau Data API"},
        {"release_version": release, "source_key": "acs_b25077",
         "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
         "release_date": None, "access_date": today,
         "tool_or_product": "Census Bureau Data API"},
        {"release_version": release, "source_key": "irs_soi",
         "vintage": f"IRS SOI county {SOI_TY}",
         "release_date": SOI_RELEASED, "access_date": today,
         "tool_or_product": "irs.gov/pub/irs-soi"},
    ], on_conflict="release_version,source_key")
    pg_upsert(sb_url, sb_key, "domain_scores", domain_rows,
              on_conflict="release_version,geoid,domain")
    pg_upsert(sb_url, sb_key, "suppression_flags", suppressed,
              on_conflict="release_version,geoid,domain")

    print(f"[wealth] done. wrote {len(domain_rows)} domain_scores, {len(suppressed)} suppression_flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
