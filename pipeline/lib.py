"""Shared helpers for domain runners. Kept minimal — extract here only what is
genuinely duplicated across two or more runners.
"""

from __future__ import annotations

import os

import requests


# Q1 territory exclusions: PR, USVI, Guam, American Samoa, N. Mariana Islands.
EXCLUDED_STATE_FIPS = {"60", "66", "69", "72", "78"}

# Census ACS sentinel values for "data not available" / suppressed.
# https://www.census.gov/programs-surveys/acs/library/handbooks/general.html
ACS_SENTINELS = {-666666666, -222222222, -333333333, -555555555,
                 -888888888, -999999999}


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def parse_acs_value(raw):
    """Coerce a Census API string to float, returning None for blanks/sentinels."""
    if raw in (None, "", "null"):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return None if v in ACS_SENTINELS else v


def upload_raw(sb_url: str, sb_key: str, release: str, source_key: str,
               filename: str, body: bytes, content_type: str = "text/csv") -> None:
    path = f"{release}/{source_key}/{filename}"
    url = f"{sb_url.rstrip('/')}/storage/v1/object/raw-extracts/{path}"
    r = requests.post(url, headers={
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }, data=body, timeout=120)
    r.raise_for_status()


def pg_upsert(sb_url: str, sb_key: str, table: str, rows: list[dict],
              on_conflict: str) -> None:
    if not rows:
        return
    url = f"{sb_url.rstrip('/')}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    for i in range(0, len(rows), 1000):
        r = requests.post(url, headers=headers, json=rows[i:i + 1000], timeout=120)
        r.raise_for_status()


def pg_delete_domain(sb_url: str, sb_key: str, table: str, release: str,
                     domain: str) -> None:
    url = (f"{sb_url.rstrip('/')}/rest/v1/{table}"
           f"?release_version=eq.{release}&domain=eq.{domain}")
    r = requests.delete(url, headers={
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}",
        "Prefer": "return=minimal",
    }, timeout=120)
    r.raise_for_status()
