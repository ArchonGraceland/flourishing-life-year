"""Education domain — three components (Q2 + Q8.3), geometric mean.

  1. ACS B15003 — share of pop 25+ with associate's degree or higher
  2. ACS C24010 — share of civilian-employed 16+ in SOC 47/49 skilled trades
  3. ACS B23006 — share of pop 25-64 with bachelor's-or-higher AND currently
     employed (Q8.3 conjunction proxy: "meaningfully employed in work that
     uses the credential"). Q8.3 amendment 2026-05-01: Q8.1.2 calls for
     credential AND deployment of credential. ACS doesn't publish a clean
     "occupation-matches-credential" cross at county level, so this uses the
     bachelor's-or-higher employment rate as the strictest county-resolvable
     proxy. Methodology page documents the trade-off (associate-degree
     holders aren't separable from "some college, no degree" in B23006).

Q5: equal weights within the domain — geometric mean of the available
component percentile ranks.
Q6 (Q8.3 update): need ≥ 2 of 3 components. With 2, geomean of available;
with ≤ 1, the domain is suppressed. (Was BOTH-of-2 before Q8.3.)

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

# B23006 — Educational Attainment by Employment Status for the Population 25-64.
# Total: _001. Bachelor's-or-higher branch:
#   _023 bachelor-or-higher subtotal
#     _024 in labor force
#       _025 in civilian labor force
#         _026 employed (civilian)
#         _027 unemployed (civilian)
#       _028 in armed forces
#     _029 not in labor force
# Q8.3 numerator = _026 + _028 (credentialed AND currently employed, civilian + AF).
# Denominator for the share = _001 (population 25-64). This yields the share of
# the working-age population that is BOTH bachelor's-or-higher AND employed —
# the strictest county-resolvable proxy for Q8.1.2's "meaningfully employed."
B23006_DENOM = "B23006_001E"
B23006_DENOM_M = "B23006_001M"
B23006_NUM = ["B23006_026E", "B23006_028E"]
B23006_NUM_M = [v.replace("E", "M") for v in B23006_NUM]

ACS_VARS = ([B15003_DENOM, B15003_DENOM_M] + B15003_NUM + B15003_NUM_M
            + [C24010_DENOM, C24010_DENOM_M] + C24010_NUM + C24010_NUM_M
            + [B23006_DENOM, B23006_DENOM_M] + B23006_NUM + B23006_NUM_M)


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
    """Return ((c1, c1_mp, c1_status), (c2, c2_mp, c2_status), (c3, c3_mp, c3_status)).

    Q1 amendment: high MOE no longer suppresses; only structural missingness
    (denom <= 0, num missing) sets a status. Otherwise status is None and
    moe_pct surfaces the noisiness.
    """
    # Component 1: associate-or-higher share among pop 25+
    num1 = sum((rec[v] or 0) for v in B15003_NUM) if all(rec[v] is not None for v in B15003_NUM) else None
    moe_num1 = combined_moe([rec[v] for v in B15003_NUM_M])
    c1, c1_moe = proportion_with_moe(num1, moe_num1, rec[B15003_DENOM], rec[B15003_DENOM_M])
    c1_status = "acs_missing" if c1 is None else None
    c1_moe_pct = (c1_moe / c1) if (c1 is not None and c1 > 0 and c1_moe is not None) else None

    # Component 2: skilled-trade share among employed civilians 16+
    num2 = sum((rec[v] or 0) for v in C24010_NUM) if all(rec[v] is not None for v in C24010_NUM) else None
    moe_num2 = combined_moe([rec[v] for v in C24010_NUM_M])
    c2, c2_moe = proportion_with_moe(num2, moe_num2, rec[C24010_DENOM], rec[C24010_DENOM_M])
    c2_status = "acs_missing" if c2 is None else None
    c2_moe_pct = (c2_moe / c2) if (c2 is not None and c2 > 0 and c2_moe is not None) else None

    # Component 3 (Q8.3): bachelor's-or-higher AND employed share among pop 25-64.
    num3 = sum((rec[v] or 0) for v in B23006_NUM) if all(rec[v] is not None for v in B23006_NUM) else None
    moe_num3 = combined_moe([rec[v] for v in B23006_NUM_M])
    c3, c3_moe = proportion_with_moe(num3, moe_num3, rec[B23006_DENOM], rec[B23006_DENOM_M])
    c3_status = "acs_missing" if c3 is None else None
    c3_moe_pct = (c3_moe / c3) if (c3 is not None and c3 > 0 and c3_moe is not None) else None

    return (c1, c1_moe_pct, c1_status), (c2, c2_moe_pct, c2_status), (c3, c3_moe_pct, c3_status)


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
    c3_values: dict[str, float] = {}
    c1_moe_pcts: dict[str, float] = {}
    c2_moe_pcts: dict[str, float] = {}
    c3_moe_pcts: dict[str, float] = {}
    suppression_by_county: dict[str, list[tuple[str, str]]] = {}

    def mark(geoid: str, source: str, reason: str) -> None:
        suppression_by_county.setdefault(geoid, []).append((source, reason))

    for rec in acs:
        geoid = rec["geoid"]
        (c1, c1_mp, c1_status), (c2, c2_mp, c2_status), (c3, c3_mp, c3_status) = compute_components(rec)
        if c1_status is None:
            c1_values[geoid] = c1
            if c1_mp is not None:
                c1_moe_pcts[geoid] = c1_mp
        else:
            mark(geoid, "acs_b15003", c1_status)
        if c2_status is None:
            c2_values[geoid] = c2
            if c2_mp is not None:
                c2_moe_pcts[geoid] = c2_mp
        else:
            mark(geoid, "acs_c24010", c2_status)
        if c3_status is None:
            c3_values[geoid] = c3
            if c3_mp is not None:
                c3_moe_pcts[geoid] = c3_mp
        else:
            mark(geoid, "acs_b23006", c3_status)

    flagged_c1 = sum(1 for mp in c1_moe_pcts.values() if mp > 0.30)
    flagged_c2 = sum(1 for mp in c2_moe_pcts.values() if mp > 0.30)
    flagged_c3 = sum(1 for mp in c3_moe_pcts.values() if mp > 0.30)
    print(f"[edu] c1 usable={len(c1_values)} (MOE>30% flagged={flagged_c1}); "
          f"c2 usable={len(c2_values)} (MOE>30% flagged={flagged_c2}); "
          f"c3 usable={len(c3_values)} (MOE>30% flagged={flagged_c3})")

    c1_ranks = percentile_rank(c1_values)
    c2_ranks = percentile_rank(c2_values)
    c3_ranks = percentile_rank(c3_values)

    domain_rows = []
    domain_flagged = 0
    suppressed: list[dict] = []
    geoids = {r["geoid"] for r in acs}
    for geoid in geoids:
        present = []
        if geoid in c1_ranks: present.append(("c1", c1_ranks[geoid], c1_moe_pcts.get(geoid)))
        if geoid in c2_ranks: present.append(("c2", c2_ranks[geoid], c2_moe_pcts.get(geoid)))
        if geoid in c3_ranks: present.append(("c3", c3_ranks[geoid], c3_moe_pcts.get(geoid)))
        if len(present) >= 2:  # Q6 (Q8.3 update): need ≥ 2 of 3
            ranks = [p[1] for p in present]
            geo = math.exp(sum(math.log(x) for x in ranks) / len(ranks))
            pct = max(1, min(100, round(geo)))
            mp_vals = [p[2] for p in present if p[2] is not None]
            mp = max(mp_vals) if mp_vals else None
            if mp and mp > 0.30:
                domain_flagged += 1
            domain_rows.append({
                "release_version": release, "geoid": geoid, "domain": DOMAIN,
                "percentile": pct, "raw_value": None,
                "moe_pct": mp,
            })
        else:
            failures = suppression_by_county.get(geoid, [])
            reason = "education_lt_2_components" if failures else "education_no_data"
            source = failures[0][0] if failures else None
            suppressed.append({
                "release_version": release, "geoid": geoid, "domain": DOMAIN,
                "reason": reason, "source_key": source,
            })
    print(f"[edu] domain_scores={len(domain_rows)} (≥2 of 3 components); "
          f"MOE>30% flagged={domain_flagged}; suppressed={len(suppressed)}")

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
    upload_raw(sb_url, sb_key, release, "acs_b23006", "B23006.csv", body)

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
        {"release_version": release, "source_key": "acs_b23006",
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
