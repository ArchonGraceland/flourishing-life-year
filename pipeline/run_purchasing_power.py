"""Purchasing power: ACS B19013 (median household income) ÷ imputed county RPP.

Per the methodology spec (Q2, twice-amended 2026-04-30):
  - Counties in an OMB-defined Metropolitan Statistical Area get that metro's BEA MARPP.
  - All other counties (non-metro + micropolitan) get their state's overall BEA SARPP.
  - BEA does NOT publish a per-state non-metropolitan RPP, only a national one (00999),
    which is why non-metro counties get the state-overall figure.

Real income = nominal_income * 100 / rpp_index (US average = 100).
"""

from __future__ import annotations

import csv
import io
import math
import os
import sys
import zipfile
from datetime import date

import requests

from lib import EXCLUDED_STATE_FIPS, load_dotenv, parse_acs_value, pg_delete_domain, pg_upsert, upload_raw
from percentile import percentile_rank

DOMAIN = "purchasing_power"
ACS_VINTAGE_YEAR = 2024
ACS_VARS = ["B19013_001E", "B19013_001M"]
RPP_YEAR = "2024"  # most recent column in BEA SARPP/MARPP files

SARPP_URL = "https://apps.bea.gov/regional/zip/SARPP.zip"
MARPP_URL = "https://apps.bea.gov/regional/zip/MARPP.zip"
OMB_CROSSWALK = "omb_cbsa_2023.csv"  # committed fixture; regenerated when OMB issues a new bulletin


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


def fetch_bea_zip(url: str) -> dict[str, bytes]:
    """Return {filename: bytes} for the CSVs inside a BEA regional zip."""
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    out = {}
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if name.endswith(".csv"):
                out[name] = z.read(name)
    return out


def parse_rpp_csv(blob: bytes, table_name: str, year: str) -> dict[str, float]:
    """Return {GeoFIPS: rpp_all_items_index} for LineCode 1 ('All items')."""
    text = blob.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    year_idx = header.index(year)
    out: dict[str, float] = {}
    for row in reader:
        if len(row) <= year_idx:
            continue
        if row[3].strip() != table_name:
            continue
        if row[4].strip() != "1":  # LineCode 1 = RPPs: All items
            continue
        geofips = row[0].strip().strip('"')
        try:
            out[geofips] = float(row[year_idx])
        except ValueError:
            continue
    return out


def load_county_to_cbsa(path: str) -> dict[str, str]:
    """{geoid: cbsa_code} for METROPOLITAN counties only.

    Micropolitan counties are intentionally absent — they fall through to state RPP
    per the spec's non-metro rule.
    """
    out: dict[str, str] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Metropolitan/Micropolitan Statistical Area"].strip() != "Metropolitan Statistical Area":
                continue
            state_fips = row["FIPS State Code"].strip()
            county_fips = row["FIPS County Code"].strip()
            cbsa = row["CBSA Code"].strip()
            if state_fips in EXCLUDED_STATE_FIPS:
                continue
            geoid = state_fips.zfill(2) + county_fips.zfill(3)
            out[geoid] = cbsa
    return out


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    census_key = os.environ["CENSUS_API_KEY"]
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]
    today = date.today().isoformat()

    print(f"[pp] release={release} fetching ACS B19013…")
    acs = fetch_acs(census_key)
    print(f"[pp] {len(acs)} county rows")

    print("[pp] fetching BEA SARPP…")
    sarpp_files = fetch_bea_zip(SARPP_URL)
    sarpp_csv = next(b for n, b in sarpp_files.items() if "SARPP_STATE" in n)
    state_rpp = parse_rpp_csv(sarpp_csv, "SARPP", RPP_YEAR)
    print(f"[pp] {len(state_rpp)} state RPP entries")

    print("[pp] fetching BEA MARPP…")
    marpp_files = fetch_bea_zip(MARPP_URL)
    marpp_csv = next(b for n, b in marpp_files.items() if "MARPP_MSA" in n)
    metro_rpp = parse_rpp_csv(marpp_csv, "MARPP", RPP_YEAR)
    print(f"[pp] {len(metro_rpp)} metro RPP entries")

    print("[pp] loading OMB CBSA crosswalk…")
    county_to_cbsa = load_county_to_cbsa(os.path.join(here, OMB_CROSSWALK))
    print(f"[pp] {len(county_to_cbsa)} metropolitan counties (micropolitan/non-metro fall through to state RPP)")

    estimates: dict[str, float] = {}
    suppressed: list[dict] = []
    no_rpp = 0

    for rec in acs:
        geoid = rec["geoid"]
        income = rec["B19013_001E"]
        moe = rec["B19013_001M"]
        if income is None or income <= 0:
            suppressed.append({"release_version": release, "geoid": geoid, "domain": DOMAIN,
                               "reason": "acs_missing", "source_key": "acs_b19013"})
            continue
        if moe is not None and (moe / income) > 0.30:
            suppressed.append({"release_version": release, "geoid": geoid, "domain": DOMAIN,
                               "reason": "acs_moe_gt_30pct", "source_key": "acs_b19013"})
            continue

        # impute county RPP
        cbsa = county_to_cbsa.get(geoid)
        if cbsa and cbsa in metro_rpp:
            rpp = metro_rpp[cbsa]
            rpp_source = "bea_marpp"
        else:
            state_geofips = geoid[:2] + "000"
            rpp = state_rpp.get(state_geofips)
            rpp_source = "bea_sarpp"

        if rpp is None or rpp <= 0:
            no_rpp += 1
            suppressed.append({"release_version": release, "geoid": geoid, "domain": DOMAIN,
                               "reason": "rpp_unavailable", "source_key": rpp_source})
            continue

        real_income = income * 100.0 / rpp  # US average = 100
        estimates[geoid] = real_income

    print(f"[pp] usable={len(estimates)} suppressed={len(suppressed)} (no_rpp={no_rpp})")

    ranks = percentile_rank(estimates)
    domain_rows = [
        {"release_version": release, "geoid": g, "domain": DOMAIN,
         "percentile": ranks[g], "raw_value": estimates[g]}
        for g in estimates
    ]

    print("[pp] uploading raw extracts…")
    # ACS raw
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["geoid", "NAME"] + ACS_VARS, extrasaction="ignore")
    w.writeheader()
    for r in acs:
        w.writerow(r)
    upload_raw(sb_url, sb_key, release, "acs_b19013", "B19013.csv", buf.getvalue().encode())
    upload_raw(sb_url, sb_key, release, "bea_sarpp", "SARPP.csv", sarpp_csv)
    upload_raw(sb_url, sb_key, release, "bea_marpp", "MARPP.csv", marpp_csv)
    with open(os.path.join(here, OMB_CROSSWALK), "rb") as f:
        upload_raw(sb_url, sb_key, release, "omb_cbsa_2023", "omb_cbsa_2023.csv", f.read())

    print("[pp] clearing prior rows for this release+domain…")
    pg_delete_domain(sb_url, sb_key, "domain_scores", release, DOMAIN)
    pg_delete_domain(sb_url, sb_key, "suppression_flags", release, DOMAIN)

    print("[pp] writing release / source_vintages / domain_scores / suppression_flags…")
    pg_upsert(sb_url, sb_key, "releases",
              [{"version": release, "released_at": today, "notes": "dev run"}],
              on_conflict="version")
    pg_upsert(sb_url, sb_key, "source_vintages", [
        {"release_version": release, "source_key": "acs_b19013",
         "vintage": f"ACS 5-yr {ACS_VINTAGE_YEAR - 4}-{ACS_VINTAGE_YEAR}",
         "release_date": None, "access_date": today,
         "tool_or_product": "Census Bureau Data API"},
        {"release_version": release, "source_key": "bea_sarpp",
         "vintage": f"BEA SARPP through {RPP_YEAR}", "release_date": None,
         "access_date": today, "tool_or_product": "BEA Regional CSV archive"},
        {"release_version": release, "source_key": "bea_marpp",
         "vintage": f"BEA MARPP through {RPP_YEAR}", "release_date": None,
         "access_date": today, "tool_or_product": "BEA Regional CSV archive"},
        {"release_version": release, "source_key": "omb_cbsa_2023",
         "vintage": "OMB July 2023 delineation (Bulletin 23-01)", "release_date": "2023-07-21",
         "access_date": today, "tool_or_product": "Census reference files"},
    ], on_conflict="release_version,source_key")
    pg_upsert(sb_url, sb_key, "domain_scores", domain_rows, on_conflict="release_version,geoid,domain")
    pg_upsert(sb_url, sb_key, "suppression_flags", suppressed, on_conflict="release_version,geoid,domain")

    print(f"[pp] done. wrote {len(domain_rows)} domain_scores, {len(suppressed)} suppression_flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
