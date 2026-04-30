"""Education domain — ACS B15003 (post-secondary credential share)
× ACS C24010 (skilled-trade occupation share), geometric mean per spec Q2.

Q5: equal weights within the domain — geometric mean of the two component
percentile ranks. Q6: BOTH components must be usable; if either is suppressed,
the whole domain is suppressed for that county.

Spec stance: "Education here means human capital, not academic credentialism.
The master electrician and the BA count equally."
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

DOMAIN = "education"
ACS_VINTAGE_YEAR = 2024

# B15003 — Educational Attainment for the Population 25 Years and Over.
# "Associate's or higher" = _021 (assoc) + _022 (bach) + _023 (master) + _024 (prof) + _025 (doc)
B15003_DENOM = "B15003_001E"
B15003_DENOM_M = "B15003_001M"
B15003_NUM = ["B15003_021E", "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E"]
B15003_NUM_M = [v.replace("E", "M") for v in B15003_NUM]

# C24010 — Sex by Occupation for Civilian Employed Population 16+.
# Skilled trades (SOC 47 + SOC 49) for both sexes:
#   _032 male construction/extraction | _033 male installation/maintenance/repair
#   _068 female construction/extraction | _069 female installation/maintenance/repair
C24010_DENOM = "C24010_001E"
C24010_DENOM_M = "C24010_001M"
C24010_NUM = ["C24010_032E", "C24010_033E", "C24010_068E", "C24010_069E"]
C24010_NUM_M = [v.replace("E", "M") for v in C24010_NUM]

ACS_VARS = ([B15003_DENOM, B15003_DENOM_M] + B15003_NUM + B15003_NUM_M
            + [C24010_DENOM, C24010_DENOM_M] + C24010_NUM + C24010_NUM_M)


def fetch_acs(api_key: str) -> list[dict]:
    """Single Census API call for all variables across both tables."""
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


def proportion_with_moe(num: float, num_moe: float, denom: float, denom_moe: float):
    """Census Handbook proportion-MOE formula (numerator subset of denominator).

    Falls back to ratio formula if the radicand goes negative.
    Returns (proportion, moe_proportion) or (None, None) if undefined.
    """
    if denom is None or denom <= 0 or num is None or num < 0:
        return None, None
    p = num / denom
    if num_moe is None:
        num_moe = 0
    if denom_moe is None:
        denom_moe = 0
    radicand = num_moe ** 2 - (p ** 2) * (denom_moe ** 2)
    if radicand >= 0:
        moe = math.sqrt(radicand) / denom
    else:
        moe = math.sqrt(num_moe ** 2 + (p ** 2) * (denom_moe ** 2)) / denom
    return p, moe


def combined_moe(moes: list[float]) -> float:
    """sqrt of sum of squared MOEs (independent estimates)."""
    return math.sqrt(sum((m or 0) ** 2 for m in moes))


def compute_components(rec: dict):
    """Return (c1, c1_status, c2, c2_status) where status is None or a reason string."""
    # Component 1: associate-or-higher share among pop 25+
    num1 = sum((rec[v] or 0) for v in B15003_NUM) if all(rec[v] is not None for v in B15003_NUM) else None
    moe_num1 = combined_moe([rec[v] for v in B15003_NUM_M])
    c1, c1_moe = proportion_with_moe(num1, moe_num1, rec[B15003_DENOM], rec[B15003_DENOM_M])
    if c1 is None:
        c1_status = "acs_missing"
    elif c1 > 0 and (c1_moe / c1) > 0.30:
        c1_status = "acs_moe_gt_30pct"
    else:
        c1_status = None

    # Component 2: skilled-trade share among employed civilians 16+
    num2 = sum((rec[v] or 0) for v in C24010_NUM) if all(rec[v] is not None for v in C24010_NUM) else None
    moe_num2 = combined_moe([rec[v] for v in C24010_NUM_M])
    c2, c2_moe = proportion_with_moe(num2, moe_num2, rec[C24010_DENOM], rec[C24010_DENOM_M])
    if c2 is None:
        c2_status = "acs_missing"
    elif c2 > 0 and (c2_moe / c2) > 0.30:
        c2_status = "acs_moe_gt_30pct"
    else:
        c2_status = None

    return c1, c1_status, c2, c2_status


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]
    today = date.today().isoformat()

    print(f"[edu] release={release} fetching ACS B15003 + C24010…")
    acs = fetch_acs(census_key)
    print(f"[edu] {len(acs)} county rows")

    c1_values: dict[str, float] = {}
    c2_values: dict[str, float] = {}
    suppressed: list[dict] = []

    for rec in acs:
        geoid = rec["geoid"]
        c1, c1_status, c2, c2_status = compute_components(rec)
        if c1_status is None:
            c1_values[geoid] = c1
        if c2_status is None:
            c2_values[geoid] = c2
        if c1_status or c2_status:
            # Q6: domain suppressed if either component fails. Record the first failing component.
            failed_source = "acs_b15003" if c1_status else "acs_c24010"
            failed_reason = c1_status or c2_status
            suppressed.append({
                "release_version": release, "geoid": geoid, "domain": DOMAIN,
                "reason": failed_reason, "source_key": failed_source,
            })

    print(f"[edu] c1 usable={len(c1_values)} c2 usable={len(c2_values)} suppressed={len(suppressed)}")

    c1_ranks = percentile_rank(c1_values)
    c2_ranks = percentile_rank(c2_values)

    domain_rows = []
    for geoid in c1_ranks:
        if geoid not in c2_ranks:
            continue
        # Geometric mean of the two component percentiles, floor 1, cap 100.
        combined = math.sqrt(c1_ranks[geoid] * c2_ranks[geoid])
        pct = max(1, min(100, round(combined)))
        domain_rows.append({
            "release_version": release, "geoid": geoid, "domain": DOMAIN,
            "percentile": pct, "raw_value": None,  # composite — no single headline number
        })
    print(f"[edu] domain_scores={len(domain_rows)} (counties with both components usable)")

    # Raw extract: a single combined CSV is fine; both tables share the same call.
    print("[edu] uploading raw extract…")
    buf = io.StringIO()
    fields = ["geoid", "NAME"] + ACS_VARS
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in acs:
        w.writerow(r)
    body = buf.getvalue().encode()
    upload_raw(sb_url, sb_key, release, "acs_b15003", "B15003.csv", body)
    upload_raw(sb_url, sb_key, release, "acs_c24010", "C24010.csv", body)

    print("[edu] clearing prior rows for this release+domain…")
    pg_delete_domain(sb_url, sb_key, "domain_scores", release, DOMAIN)
    pg_delete_domain(sb_url, sb_key, "suppression_flags", release, DOMAIN)

    print("[edu] writing release / source_vintages / domain_scores / suppression_flags…")
    pg_upsert(sb_url, sb_key, "releases",
              [{"version": release, "released_at": today, "notes": "dev run"}],
              on_conflict="version")
    pg_upsert(sb_url, sb_key, "source_vintages", [
        {"release_version": release, "source_key": "acs_b15003",
         "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
         "release_date": None, "access_date": today,
         "tool_or_product": "Census Bureau Data API"},
        {"release_version": release, "source_key": "acs_c24010",
         "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
         "release_date": None, "access_date": today,
         "tool_or_product": "Census Bureau Data API"},
    ], on_conflict="release_version,source_key")
    pg_upsert(sb_url, sb_key, "domain_scores", domain_rows,
              on_conflict="release_version,geoid,domain")
    pg_upsert(sb_url, sb_key, "suppression_flags", suppressed,
              on_conflict="release_version,geoid,domain")

    print(f"[edu] done. wrote {len(domain_rows)} domain_scores, {len(suppressed)} suppression_flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
