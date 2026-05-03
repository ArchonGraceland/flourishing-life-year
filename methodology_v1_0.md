# Methodology v1.0 — per-person loss-weighting + county lived FLY

**Status:** *Adopted 2026-05-03. Soak review 2026-05-15. Locks after soak.*
**Supersedes:** v0.9 (PUMS person-level FLY) for per-person and county-level scoring.
**Preserves:** v0.9 national aggregate methodology, frozen-2019 thresholds, additive structure for derived raw totals. v0.9 percentile-rank composite remains available on the map as a "v0.9 legacy" toggle for transparency.

---

## What changes

1. **Per-person FLY scoring** — Below contributions are no longer silent. They actively subtract from the score, weighted 2× per Kahneman & Tversky 1992 loss aversion (median λ ≈ 2.25, rounded to 2.0 for clarity).
2. **County-level composite on the map** — switches from cross-sectional **percentile-rank geomean** (v0.9) to per-person **lived FLY in 0–1 FLY space** (v1.0), aggregated up from PUMS person-level scoring.
3. **Methodology unification** — loss-weighting now applies uniformly to *changes from baseline* at every scale (per-person, per-county, national) instead of only at national aggregate. The same rule runs from the assessment to the headline.

## What does NOT change

- The five domains, their indicators, or the frozen-2019 absolute thresholds (Strong / Adequate / Below remain identically defined).
- The national raw additive total — still reported alongside the lived total for audit.
- The principle that **levels stay raw, changes get loss-weighted**. v1.0 just extends the scope of "change" to include the per-person deviation from the Adequate baseline. (See § *Why this is internally consistent* below.)
- The cost-per-FLY methodology in §05 (already lived-FLY-based after the recent §05 update).

---

## v1.0 per-person FLY — formal definition

For each person, with five domains scored Strong / Adequate / Below against the frozen-2019 absolute thresholds:

```
deviation_d = +0.10  if Strong
              0      if Adequate
             −0.10   if Below

raw FLY    = 0.20 × #Strong + 0.10 × #Adequate           (unchanged from v0.9)
lived FLY  = 0.50 + Σ_d ( deviation_d × w_d )
   where  w_d = 1   if deviation_d ≥ 0
                2   if deviation_d  < 0     (loss-weighting)
```

**Range under v1.0:**
- 5 Strong → lived FLY = 0.50 + 5 × 0.10 = **+1.00**
- 5 Adequate → lived FLY = **0.50** (matches v0.9 raw)
- 5 Below → lived FLY = 0.50 + 5 × (−0.20) = **−0.50**

The 5-Strong and 5-Adequate endpoints match v0.9 raw FLY. The 5-Below endpoint is now negative (−0.50 vs 0.00 in v0.9), reflecting that complete failure on every domain isn't a "zero contribution" — it's an active loss against the baseline. Mixed cases shift downward in proportion to the count of Below domains.

### Worked example (the "doing great except wealth" case)

```
4 Strong + 1 Below in wealth:
  raw FLY   = 4 × 0.20 + 0      = 0.80
  lived FLY = 0.50 + 4 × 0.10 + 1 × (−0.20) = 0.70
```

The 0.10 drag captures the lived cost of one weak domain that the additive ledger silently absorbs.

### Why this is internally consistent

Loss-aversion was defined in v0.9-era doc as applying to *changes from baseline*. v1.0 makes the per-person scoring also a change-from-baseline:

```
v1.0 reframing:
  baseline = 0.50  ("everyone Adequate" reference point)
  per-person FLY = baseline + Σ deviations
```

Loss-weighting applies to the deviations. This is the same loss-aversion principle as v0.9-aggregate, just at a different scale.

---

## v1.0 county-level lived FLY (Option B) — formal definition

For each county *c*:

```
lived_FLY_county(c) = Σ_{p ∈ pop(c)} weight_p × lived_FLY_person(p)
                   ÷ Σ_{p ∈ pop(c)} weight_p
```

Where `pop(c)` is the population of county *c* and `weight_p` is the PUMS person weight (PWGTP). This is the population-weighted mean of per-person v1.0 lived FLY.

**Range:** [−0.50, +1.00], same as per-person.
**Color scale on the map:** 0–1 FLY space, with anchors at 0.40 (struggling), 0.60 (mixed), 0.80 (doing well). Counties with negative composites are flagged in deep accent.
**Comparability:** v0.9 percentile-rank composite (1–100) and v1.0 lived FLY (−0.50 to +1.00) are *different metrics*. Counties cannot be ranked the same way across the two. v0.9 stays available as a secondary toggle ("Compare v0.9 percentile composite") for transparency and continuity, but v1.0 lived FLY is the headline map metric.

---

## PUMA proxy for county

PUMS data resolves to **PUMA** (Public Use Microdata Area), not county. Each PUMA covers ≥100,000 residents. Most PUMAs map cleanly to a single county or a metro-area cluster of counties; some PUMAs straddle county lines.

**Proxy rule:**
- Counties wholly inside a single PUMA → take that PUMA's lived FLY directly.
- Counties straddling multiple PUMAs → population-weighted mean across the overlapping PUMAs (using Census MABLE/Geocorr or 2020 PUMA-county crosswalk).
- Multi-county PUMAs → all counties in the PUMA share the same lived FLY value (the imperfection is documented per-county in the tooltip: "PUMA-level estimate, sub-county variation not resolved").

**Documented limitation:** sub-county variation is not resolvable under PUMS. This is the cost of using PUMS at all for sub-national resolution; the alternative (no PUMS) means losing person-level scoring entirely. v1.0 accepts this trade-off and discloses it on every county tooltip in the affected counties.

---

## Reliability flag for small PUMAs

Borrowing the existing ACS MOE > 30% pattern (v0.9 amendment): counties whose underlying PUMS sample is below a threshold get a reliability flag. **Threshold = `n < 1000` PUMS person records** (selected after a soak-period sweep against 2024 data, locked 2026-05-03). At n=1000 the standard error on a binary share is ≈ ±3pp at 95% CI — a meaningful resolution floor. The 1000 cut yields ~1.2% flag rate on the v2025.12 release (39 of 3,143 counties), making the flag a genuine "undersized PUMA" warning rather than a soft caution that flags half the country. Earlier proposals at 1,500 (~26% flag rate) were rejected for fighting the visual signal of the percentile-stretched map palette.

Flagged counties:
- Render at 65% opacity on the map (matches v0.9 reliability-flag treatment).
- Tooltip surfaces the actual PUMA-level PUMS sample size next to a "small-PUMA flag" note.
- Composite is computed and ranked, but flagged so a reader can decide.

Suppression (gray on the map) is reserved for PUMAs without sufficient PUMS responses to compute any per-domain scoring — same conceptual rule as v0.9 (data unavailability ≠ noise).

---

## Schema design — `county_lived_fly`

New Supabase table per release version (does not replace `composite_scores`; runs alongside it for backward compat):

```sql
create table county_lived_fly (
  geoid              text not null,
  release_version    text not null,
  lived_fly          numeric(4, 3),    -- −0.500 to +1.000
  raw_fly            numeric(4, 3),    --  0.000 to +1.000
  n_strong           smallint,         -- 0 to 5
  n_adequate         smallint,
  n_below            smallint,
  domains_resolved   smallint,         -- ≥ 4 required, else suppressed
  puma_source        text,             -- e.g. "0103200" or "multi-PUMA pop-weighted"
  pums_sample_size   integer,
  reliability_flag   boolean,          -- true if pums_sample_size < threshold
  primary key (geoid, release_version)
);

create index county_lived_fly_release on county_lived_fly (release_version);
```

`domain_scores` is unchanged (still per-county per-domain percentile rank for the per-domain map views). Only the composite metric changes between v0.9 and v1.0.

---

## Backward compatibility and rollout

- **v0.9 stays fully readable.** `composite_scores` table is preserved; the v0.9 percentile composite is reachable via a "v0.9 (legacy)" toggle on the map.
- **Map default view** flips to v1.0 lived FLY.
- **Per-domain views** unchanged (still percentile rank in 1–100 space, since per-domain pipelines aren't affected).
- **Tooltip language** updated to make clear the metric: "Lived FLY: 0.71 — population-weighted from PUMA 0103200."
- **Methodology page** linked from each tooltip + the §why-losses page.

---

## Migration plan (phased)

| Phase | What | Status |
|------|------|--------|
| 1 | Methodology amendment doc + frontend mocks | ✅ done 2026-05-03 |
| 2 | Schema migration: `county_lived_fly` table in Supabase | ✅ done — migration `20260503133200_add_county_lived_fly` |
| 3 | PUMS pipeline: PUMA-level scoring + PUMA→county crosswalk + lived FLY computation | ✅ done — `pipeline/run_pums_county.py`, validated end-to-end on VT then full 50-state dry-run |
| 4 | First v1.0 release: load `county_lived_fly` for release `v2025.12` | ✅ done — 3,143 rows live, lived FLY 0.267→0.855, mean 0.639, drag 0.063 vs raw |
| 5 | Frontend swap to live v1.0 data; v0.9 demoted to legacy toggle | ✅ done — `loadMap` auto-detects `county_lived_fly` rows and flips banner/button/tooltip |
| 6 | Methodology spec amendment finalized; v1.0 locked | pending soak review 2026-05-15 |

---

## What you'll see in the mock (phase 1)

The map currently has a new view toggle: **"v1.0 lived FLY (mock)"**. When selected:

- County composite is recomputed client-side from the existing per-domain percentile data using a rough Strong/Adequate/Below mapping (pct ≥ 75 = Strong; 25 ≤ pct < 75 = Adequate; pct < 25 = Below). This is **deterministic** and **illustrative only** — the real v1.0 pipeline runs against PUMS person-level data with frozen-2019 absolute thresholds, not against percentile ranks.
- Color scale shifts to 0–1 FLY space.
- Tooltip shows raw FLY + lived FLY + below-count.
- A prominent banner above the map: **"MOCK — synthesized from v0.9 percentiles for visual review. Real v1.0 data ships with the pipeline rebuild."**

The mock is calibrated to look directionally correct, not numerically precise. It exists so you can see the visual + UX impact before authorizing the pipeline + schema work.

---

## Risks and open questions

- **PUMA-county crosswalk reliability for split PUMAs.** Multi-county PUMAs flatten to a single value across all member counties. Some metro-area PUMAs span 3+ counties; that's a real loss of resolution. Document per-county.
- **Negative-composite counties.** Some counties under v1.0 will score below zero. Map color scale needs to handle this without making the country look worse than it is — proposed: clamp display to [−0.50, +1.00] with the negative band rendered in deepening accent.
- **Trend chart at county level.** Out of scope for v1.0 launch. Per-county lived FLY trend (year-over-year at county resolution) would require historical PUMS rebuild — flag for v1.1.
- **Headline narrative shift.** Currently the map's headline number is the *best* counties' percentile-rank score (~95). Under v1.0, the best counties might score ~0.85 lived FLY. Reframing the marketing copy is needed.

---

## Reference

- Kahneman, D. & Tversky, A. (1992). *Advances in Prospect Theory: Cumulative Representation of Uncertainty*. Journal of Risk and Uncertainty 5(4): 297–323.
- Census Bureau, *2020 PUMA-County Crosswalk* (Geocorr 2020 / MABLE).
- v0.9 PUMS methodology (`methodology_v09_pums.md`) — preserved as the per-person scoring foundation; v1.0 layers the loss-weighting on top.
