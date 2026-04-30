"""Percentile rank with a floor of 1 (never 0) — Q5 of the methodology spec.

Floor prevents geometric-mean collapse on the worst county.
Returns integers 1..100. Ties get the same percentile (average rank method).
Higher input value -> higher percentile, unless `invert=True` (used for mortality).
"""

from __future__ import annotations


def percentile_rank(values: dict[str, float], invert: bool = False) -> dict[str, int]:
    items = [(geoid, v) for geoid, v in values.items() if v is not None]
    if not items:
        return {}
    items.sort(key=lambda kv: kv[1], reverse=invert)
    n = len(items)
    out: dict[str, int] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        pct = max(1, min(100, round(100 * avg_rank / n)))
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


if __name__ == "__main__":
    sample = {f"c{i}": float(i) for i in range(100)}
    ranks = percentile_rank(sample)
    assert min(ranks.values()) >= 1
    assert max(ranks.values()) <= 100
    assert ranks["c0"] == 1
    assert ranks["c99"] == 100
    inv = percentile_rank(sample, invert=True)
    assert inv["c0"] == 100
    assert inv["c99"] == 1
    print("ok")
