"""Pilot end-to-end: family domain (ACS B09005, "children under 18 with two parents").

Fetches ACS 5-year county data, applies MOE>30% suppression, percentile-ranks,
uploads the raw extract to the raw-extracts bucket, and writes to
releases / source_vintages / domain_scores / suppression_flags.

Run locally with .env loaded, or via the GitHub Actions workflow.
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

ACS_VINTAGE_YEAR = 2024  # 5-year endpoint covers 2020-2024 (released Dec 2025)
ACS_TABLE = "B09005"
SOURCE_KEY = "acs_b09005"
DOMAIN = "family"

# B09005 — Household Type for Children Under 18 Years (post-2019 schema).
#   _001 total | _002 married-couple | _003 cohabiting couple
#   _004/_005 = single parent.
# Q8.6 (2026-05-01): family-domain numerator is married-couple ONLY (B09005_002).
# This reverses the Q2 third amendment's inclusion of cohabiting couples (_003).
# Per Q8.1.5 the index anchors family-domain flourishing on stable married-couple
# household structure; cohabiting two-parent households are not counted toward
# the criterion. Cohabiting-couple data is still pulled (visible in raw extract)
# for transparency and for any future analysis, but does NOT enter the indicator.
ACS_VARS = ["B09005_001E", "B09005_001M",
            "B09005_002E", "B09005_002M",
            "B09005_003E", "B09005_003M"]   # _003 retained in raw for transparency


def fetch_acs(api_key: str) -> list[dict]:
    url = f"https://api.census.gov/data/{ACS_VINTAGE_YEAR}/acs/acs5"
    params = {"get": "NAME," + ",".join(ACS_VARS), "for": "county:*", "key": api_key}
    r = requests.get(url, params=params, timeout=120)
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


def compute_indicator(rec: dict) -> tuple[float | None, float | None, str | None]:
    """Return (estimate, moe, suppression_reason).

    Q8.6 indicator: married-couple share of children under 18 (B09005_002 / B09005_001).
    Cohabiting-couple households (B09005_003) are NOT included — Q8.1.5 anchors family-domain
    flourishing on stable married-couple household structure.

    Uses Census Handbook proportion-MOE formula since numerator (married-couple kids) is
    a subset of denominator (all kids).
    """
    total = rec["B09005_001E"]
    married = rec["B09005_002E"]
    if total is None or total <= 0 or married is None:
        return None, None, "acs_missing"
    if married <= 0:
        return None, None, "acs_missing"
    p = married / total
    m_num = rec["B09005_002M"] or 0
    m_total = rec["B09005_001M"] or 0
    radicand = m_num ** 2 - (p ** 2) * (m_total ** 2)
    if radicand >= 0:
        moe = math.sqrt(radicand) / total
    else:
        moe = math.sqrt(m_num ** 2 + (p ** 2) * (m_total ** 2)) / total
    # Q1 amendment: high MOE no longer suppresses; surfaced as moe_pct instead.
    return p, moe, None


def to_raw_csv(records: list[dict]) -> bytes:
    buf = io.StringIO()
    fields = ["geoid", "NAME"] + ACS_VARS
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in records:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def main() -> int:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]

    print(f"[family] release={release} fetching ACS {ACS_TABLE} ({ACS_VINTAGE_YEAR} 5-yr)…")
    raw = fetch_acs(census_key)
    print(f"[family] fetched {len(raw)} county rows")

    estimates: dict[str, float] = {}
    moe_pcts: dict[str, float] = {}
    suppressed: list[dict] = []
    flagged = 0
    for rec in raw:
        est, moe, reason = compute_indicator(rec)
        if reason:
            suppressed.append({
                "release_version": release,
                "geoid": rec["geoid"],
                "domain": DOMAIN,
                "reason": reason,
                "source_key": SOURCE_KEY,
            })
        else:
            estimates[rec["geoid"]] = est
            if est is not None and est > 0 and moe is not None:
                mp = moe / est
                moe_pcts[rec["geoid"]] = mp
                if mp > 0.30:
                    flagged += 1

    print(f"[family] usable={len(estimates)} suppressed={len(suppressed)}; MOE>30% flagged={flagged}")

    ranks = percentile_rank(estimates)
    domain_rows = [
        {"release_version": release, "geoid": g, "domain": DOMAIN,
         "percentile": ranks[g], "raw_value": estimates[g],
         "moe_pct": moe_pcts.get(g)}
        for g in estimates
    ]

    print("[family] uploading raw extract…")
    upload_raw(sb_url, sb_key, release, SOURCE_KEY, f"{ACS_TABLE}.csv", to_raw_csv(raw))

    print("[family] clearing prior rows for this release+domain…")
    pg_delete_domain(sb_url, sb_key, "domain_scores", release, DOMAIN)
    pg_delete_domain(sb_url, sb_key, "suppression_flags", release, DOMAIN)

    print("[family] writing release / source_vintages / domain_scores / suppression_flags…")
    pg_upsert(sb_url, sb_key, "releases",
              [{"version": release, "released_at": date.today().isoformat(),
                "notes": "dev run"}],
              on_conflict="version")
    pg_upsert(sb_url, sb_key, "source_vintages",
              [{"release_version": release, "source_key": SOURCE_KEY,
                "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
                "release_date": None,
                "access_date": date.today().isoformat(),
                "tool_or_product": "Census Bureau Data API"}],
              on_conflict="release_version,source_key")
    pg_upsert(sb_url, sb_key, "domain_scores", domain_rows,
              on_conflict="release_version,geoid,domain")
    pg_upsert(sb_url, sb_key, "suppression_flags", suppressed,
              on_conflict="release_version,geoid,domain")

    print(f"[family] done. wrote {len(domain_rows)} domain_scores, {len(suppressed)} suppression_flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
