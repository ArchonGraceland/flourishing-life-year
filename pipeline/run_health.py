"""Health domain — all-cause age-adjusted mortality from CDC WONDER UCD 1999-2020.

Per spec Q2 (third amendment 2026-04-30): USALEEP is dropped from v0.x because NCHS
hasn't refreshed it past 2010-2015. The original "mortality fallback" becomes the
headline. CDC WONDER UCD 1999-2020 is the canonical product (per Q3 release pinning).

Suppression cascade:
  - WONDER's own threshold (n ≤ 9) — already excluded from the export.
  - Spec reliability rule (n < 20) — applied here.
  - Direction correction: mortality higher = worse; invert before percentile-ranking
    so the domain reads "higher score = better."

The export is a manual UI download (CDC WONDER does not allow county-grouped queries
via the API). It lives at pipeline/wonder_ucd_1999_2020_county.tsv as a committed
fixture; regenerate when cutting a new release version.
"""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import date

import requests

from lib import EXCLUDED_STATE_FIPS, load_dotenv, pg_delete_domain, pg_upsert, upload_raw
from percentile import percentile_rank

DOMAIN = "health"
SOURCE_KEY = "cdc_wonder_ucd"
WONDER_FIXTURE = "wonder_ucd_1999_2020_county.tsv"
RELIABILITY_MIN_DEATHS = 20  # Q1 spec rule: n < 20 -> suppress (on top of WONDER's own n≤9)


def parse_wonder_tsv(path: str):
    """Yield {geoid, deaths, population, age_adj_rate} for each county data row.

    WONDER's export has a Notes column at position 0; data rows have empty Notes,
    footnote rows have content there. Stop iterating at the first non-empty Notes row.
    Header column order is fixed: Notes | County | County Code | Deaths | Population
                                  | Crude Rate | Age Adjusted Rate
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quotechar='"')
        header = next(reader)
        assert header[:7] == ["Notes", "County", "County Code", "Deaths", "Population",
                              "Crude Rate", "Age Adjusted Rate"], header
        for row in reader:
            if not row or len(row) < 7:
                continue
            if row[0]:  # Notes column populated -> footnote section starts
                break
            geoid = row[2].strip()
            if len(geoid) != 5 or not geoid.isdigit():
                continue
            if geoid[:2] in EXCLUDED_STATE_FIPS:
                continue
            try:
                deaths = int(row[3])
                population = int(row[4])
                age_adj = float(row[6]) if row[6] not in ("", "Suppressed", "Unreliable") else None
            except ValueError:
                continue
            yield {"geoid": geoid, "deaths": deaths,
                   "population": population, "age_adj_rate": age_adj}


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]
    today = date.today().isoformat()
    fixture_path = os.path.join(here, WONDER_FIXTURE)

    print(f"[health] release={release} reading {WONDER_FIXTURE}…")
    rows = list(parse_wonder_tsv(fixture_path))
    print(f"[health] {len(rows)} county data rows")

    estimates: dict[str, float] = {}
    suppressed: list[dict] = []
    for r in rows:
        if r["age_adj_rate"] is None:
            suppressed.append({"release_version": release, "geoid": r["geoid"], "domain": DOMAIN,
                               "reason": "wonder_unreliable_or_suppressed",
                               "source_key": SOURCE_KEY})
        elif r["deaths"] < RELIABILITY_MIN_DEATHS:
            suppressed.append({"release_version": release, "geoid": r["geoid"], "domain": DOMAIN,
                               "reason": "wonder_n_lt_20",
                               "source_key": SOURCE_KEY})
        else:
            estimates[r["geoid"]] = r["age_adj_rate"]

    print(f"[health] usable={len(estimates)} suppressed={len(suppressed)}")

    # Direction correction (Q5): mortality is "higher = worse," so percentile-rank with invert=True.
    ranks = percentile_rank(estimates, invert=True)
    domain_rows = [
        {"release_version": release, "geoid": g, "domain": DOMAIN,
         "percentile": ranks[g], "raw_value": estimates[g]}
        for g in estimates
    ]

    print("[health] uploading raw extract…")
    with open(fixture_path, "rb") as f:
        upload_raw(sb_url, sb_key, release, SOURCE_KEY, WONDER_FIXTURE, f.read(),
                   content_type="text/tab-separated-values")

    print("[health] clearing prior rows for this release+domain…")
    pg_delete_domain(sb_url, sb_key, "domain_scores", release, DOMAIN)
    pg_delete_domain(sb_url, sb_key, "suppression_flags", release, DOMAIN)

    print("[health] writing release / source_vintages / domain_scores / suppression_flags…")
    pg_upsert(sb_url, sb_key, "releases",
              [{"version": release, "released_at": today, "notes": "dev run"}],
              on_conflict="version")
    pg_upsert(sb_url, sb_key, "source_vintages",
              [{"release_version": release, "source_key": SOURCE_KEY,
                "vintage": "CDC WONDER UCD 1999-2020 (final), all-cause age-adjusted",
                "release_date": None, "access_date": today,
                "tool_or_product": "CDC WONDER Online Database UCD 1999-2020 (D76)"}],
              on_conflict="release_version,source_key")
    pg_upsert(sb_url, sb_key, "domain_scores", domain_rows,
              on_conflict="release_version,geoid,domain")
    pg_upsert(sb_url, sb_key, "suppression_flags", suppressed,
              on_conflict="release_version,geoid,domain")

    print(f"[health] done. wrote {len(domain_rows)} domain_scores, {len(suppressed)} suppression_flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
