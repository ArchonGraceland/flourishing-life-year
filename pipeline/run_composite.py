"""Composite — geometric mean of available domain percentile ranks.

Per spec Q6:
  - With ≥ 4 of 5 domains available: composite = geomean(available); county is colored.
  - With ≤ 3 of 5 available: composite = NULL; county is gray on the map.

Reads from domain_scores; writes to composite_scores.
"""

from __future__ import annotations

import math
import os
import sys
from datetime import date

import requests

from lib import load_dotenv, pg_upsert

DOMAINS = ["family", "purchasing_power", "education", "wealth", "health"]


def fetch_domain_scores(sb_url: str, sb_key: str, release: str) -> dict[str, dict[str, int]]:
    """{geoid: {domain: percentile}} for the given release."""
    url = (f"{sb_url.rstrip('/')}/rest/v1/domain_scores"
           f"?release_version=eq.{release}&select=geoid,domain,percentile")
    headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    out: dict[str, dict[str, int]] = {}
    # PostgREST default page size is 1000; paginate until we drain.
    offset = 0
    page = 1000
    while True:
        h = dict(headers); h["Range-Unit"] = "items"; h["Range"] = f"{offset}-{offset + page - 1}"
        r = requests.get(url, headers=h, timeout=120)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            out.setdefault(row["geoid"], {})[row["domain"]] = row["percentile"]
        if len(rows) < page:
            break
        offset += page
    return out


def pg_delete_release(sb_url: str, sb_key: str, table: str, release: str) -> None:
    url = f"{sb_url.rstrip('/')}/rest/v1/{table}?release_version=eq.{release}"
    r = requests.delete(url, headers={
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Prefer": "return=minimal",
    }, timeout=120)
    r.raise_for_status()


def main() -> int:
    here = os.path.dirname(__file__)
    load_dotenv(os.path.join(here, "..", ".env"))
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    release = os.environ["RELEASE_VERSION"]

    print(f"[composite] release={release} fetching domain_scores…")
    scores = fetch_domain_scores(sb_url, sb_key, release)
    print(f"[composite] {len(scores)} counties have at least one domain score")

    rows = []
    coverage = {n: 0 for n in range(6)}
    for geoid, by_domain in scores.items():
        ranks = [by_domain[d] for d in DOMAINS if d in by_domain]
        n = len(ranks)
        coverage[n] += 1
        if n >= 4:
            geo = math.exp(sum(math.log(x) for x in ranks) / n)
            comp = round(geo, 4)  # numeric; rounded to keep payload small
        else:
            comp = None
        rows.append({
            "release_version": release, "geoid": geoid,
            "composite": comp, "domains_available": n,
        })

    print(f"[composite] coverage by n_avail: " +
          ", ".join(f"{n}={coverage[n]}" for n in sorted(coverage) if coverage[n]))
    eligible = sum(coverage[n] for n in (4, 5))
    print(f"[composite] eligible (≥4 of 5): {eligible}")

    print("[composite] clearing prior rows…")
    pg_delete_release(sb_url, sb_key, "composite_scores", release)

    print("[composite] writing composite_scores…")
    pg_upsert(sb_url, sb_key, "composite_scores", rows,
              on_conflict="release_version,geoid")
    print(f"[composite] done. wrote {len(rows)} composite_scores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
