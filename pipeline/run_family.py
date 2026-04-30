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

from percentile import percentile_rank


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ACS_VINTAGE_YEAR = 2024  # 5-year endpoint covers 2020-2024 (released Dec 2025)
ACS_TABLE = "B09005"
SOURCE_KEY = "acs_b09005"
DOMAIN = "family"

# B09005 — Household Type for Children Under 18 Years (post-2019 schema).
#   _001 total | _002 married-couple | _003 cohabiting couple
#   _004/_005 = single parent. "Two parents" = _002 + _003.
ACS_VARS = ["B09005_001E", "B09005_001M",
            "B09005_002E", "B09005_002M",
            "B09005_003E", "B09005_003M"]


# Territories excluded per Q1 of methodology spec: PR=72, USVI=78, GU=66, AS=60, MP=69.
EXCLUDED_STATE_FIPS = {"60", "66", "69", "72", "78"}


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
            rec[v] = float(rec[v]) if rec[v] not in (None, "", "null") else None
        out.append(rec)
    return out


def compute_indicator(rec: dict) -> tuple[float | None, float | None, str | None]:
    """Return (estimate, moe, suppression_reason).

    Uses Census Handbook proportion-MOE formula since numerator (two-parent kids)
    is a subset of denominator (all kids). If the radicand goes negative, fall
    back to the ratio formula (Handbook's documented escape).
    """
    total = rec["B09005_001E"]
    married = rec["B09005_002E"]
    cohab = rec["B09005_003E"]
    if total is None or total <= 0 or married is None or cohab is None:
        return None, None, "acs_missing"
    num = married + cohab
    if num <= 0:
        return None, None, "acs_missing"
    p = num / total
    m_married = rec["B09005_002M"] or 0
    m_cohab = rec["B09005_003M"] or 0
    m_num = math.sqrt(m_married ** 2 + m_cohab ** 2)  # MOE for sum of two estimates
    m_total = rec["B09005_001M"] or 0
    radicand = m_num ** 2 - (p ** 2) * (m_total ** 2)
    if radicand >= 0:
        moe = math.sqrt(radicand) / total
    else:
        moe = math.sqrt(m_num ** 2 + (p ** 2) * (m_total ** 2)) / total
    if p > 0 and (moe / p) > 0.30:
        return p, moe, "acs_moe_gt_30pct"
    return p, moe, None


def to_raw_csv(records: list[dict]) -> bytes:
    buf = io.StringIO()
    fields = ["geoid", "NAME"] + ACS_VARS
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in records:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def upload_raw(supabase_url: str, key: str, release_version: str, body: bytes) -> str:
    path = f"{release_version}/{SOURCE_KEY}/{ACS_TABLE}.csv"
    url = f"{supabase_url}/storage/v1/object/raw-extracts/{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "text/csv",
        "x-upsert": "true",
    }
    r = requests.post(url, headers=headers, data=body, timeout=60)
    r.raise_for_status()
    return path


def pg_upsert(supabase_url: str, key: str, table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    url = f"{supabase_url}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    # batch in chunks to keep request bodies reasonable
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        r = requests.post(url, headers=headers, json=chunk, timeout=120)
        r.raise_for_status()


def main() -> int:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"].rstrip("/")
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]

    print(f"[family] release={release} fetching ACS {ACS_TABLE} ({ACS_VINTAGE_YEAR} 5-yr)…")
    raw = fetch_acs(census_key)
    print(f"[family] fetched {len(raw)} county rows")

    estimates: dict[str, float] = {}
    suppressed: list[dict] = []
    for rec in raw:
        est, _moe, reason = compute_indicator(rec)
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

    print(f"[family] usable={len(estimates)} suppressed={len(suppressed)}")

    ranks = percentile_rank(estimates)
    domain_rows = [
        {"release_version": release, "geoid": g, "domain": DOMAIN,
         "percentile": ranks[g], "raw_value": estimates[g]}
        for g in estimates
    ]

    print("[family] uploading raw extract…")
    upload_raw(sb_url, sb_key, release, to_raw_csv(raw))

    print("[family] clearing prior rows for this release+domain…")
    # delete any existing rows for (release, domain) so a re-run is fully fresh.
    for table in ("domain_scores", "suppression_flags"):
        del_url = (f"{sb_url}/rest/v1/{table}"
                   f"?release_version=eq.{release}&domain=eq.{DOMAIN}")
        r = requests.delete(del_url, headers={
            "apikey": sb_key, "Authorization": f"Bearer {sb_key}",
            "Prefer": "return=minimal",
        }, timeout=120)
        r.raise_for_status()

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
