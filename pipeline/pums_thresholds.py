"""Frozen 2019-anchored thresholds for PUMS-based person-level FLY scoring.

The new methodology (v0.8): every person in PUMS gets a Strong/Adequate/Below
rating per domain against absolute, frozen-2019 thresholds. Person FLY is
0.20 × #Strong + 0.10 × #Adequate + 0.00 × #Below (range 0.0–1.0). Total
national FLY = Σ over PUMS records of PWGTP × person_FLY.

Thresholds are anchored at 2019 cuts and applied unchanged forward, with
nominal-dollar quantities CPI-deflated when comparing later years.

Spec rationale recap (locked 2026-05-02):
  - Approach B (per-domain partial credit, not per-person tier collapse).
  - Health is downgraded one tier when PUMS DIS = disabled.
  - Family is age-banded (a 20-year-old single, a 40-year-old married, and a
    70-year-old widowed are each in valid Strong configurations).
  - Purchasing-power Strong bar = 1.10 × SPM (10% margin to "save some").
  - Wealth uses PUMS tenure + housing value as proxy; IRS SOI by ZIP is the
    secondary triangulation but not used at PUMS scoring (county-level only).
  - Cohort definition: NONE — these bars are absolute, not cohort-relative.
    Growth shows up; redistribution shows up; both cleanly.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Purchasing power: SPM-equivalent thresholds by household size and year.
#
# Reference threshold = Census Supplemental Poverty Measure 2-adult-2-child
# threshold for owner with mortgage (the modal SPM category), national average,
# from the annual Census P60 Supplemental Poverty Measure publication.
#
# Equivalence scale: SPM uses (NA + 0.5 * NC) ** 0.7 with reference scale
# (2 + 0.5*2)^0.7 = 3^0.7. We derive (NA, NC) from PUMS person records grouped
# by SERIALNO using AGEP < 18 as the child cutoff.
# ---------------------------------------------------------------------------

SPM_REFERENCE_2A2C = {
    # Year: Census P60 reference 2-adult-2-child SPM threshold (owner-with-mortgage,
    # national average). Sources: Fox & Burns / Census P60-285 (2019, 2020),
    # P60-275 (2021), P60-280 (2022), P60-291 (2023), P60-298 (2024).
    2019: 28_881,
    2020: 29_330,
    2021: 31_453,
    2022: 34_518,
    2023: 36_482,
    2024: 37_502,
}

# Equivalence scale exponent and reference normalization.
SPM_EQUIV_EXPONENT = 0.7
SPM_REFERENCE_SCALE = 3 ** SPM_EQUIV_EXPONENT  # ≈ 2.158


def spm_threshold(num_adults: int, num_children: int, year: int) -> float:
    """SPM threshold for a household of (NA adults, NC children) in `year`."""
    base = SPM_REFERENCE_2A2C[year]
    equiv = (num_adults + 0.5 * num_children) ** SPM_EQUIV_EXPONENT
    return base * equiv / SPM_REFERENCE_SCALE


# ---------------------------------------------------------------------------
# Purchasing power tier rule
# ---------------------------------------------------------------------------

PP_STRONG_MULTIPLIER = 1.10  # "10% above SPM = pays bills + saves some"


def score_purchasing_power(hh_income, num_adults, num_children, year):
    """Strong / Adequate / Below for a household, replicated to each person in it."""
    if hh_income is None:
        return None
    threshold = spm_threshold(num_adults, num_children, year)
    if hh_income < threshold:
        return "below"
    if hh_income >= threshold * PP_STRONG_MULTIPLIER:
        return "strong"
    return "adequate"


# ---------------------------------------------------------------------------
# Wealth: PUMS tenure + housing value, age-aware.
#
# Variables:
#   TEN  — 1 owned with mortgage, 2 owned free and clear, 3 rented, 4 occupied no rent
#   VALP — housing value (owners only); use as wealth proxy
#   AGEP — person age, used to age-adjust the bar (a 70-year-old paid-off
#          homeowner is Strong; a 25-year-old renter is not Below by default)
#
# Honest caveat: PUMS does not give 401(k) balances. This is a proxy, not a
# direct measurement. The county/IRS SOI cross-check belongs in a county-level
# pass; here we score at the person level using HH-housing signal only.
# ---------------------------------------------------------------------------

# CPI-deflate VALP threshold to constant-2019 dollars before comparing.
# The "substantial equity" line is set at 2019 national median home value
# (Zillow / Census ACS B25077): $240,500 in 2019 dollars.
VALP_STRONG_2019 = 240_500
VALP_ADEQUATE_2019 = 100_000  # rough "owner with at least mid-range home" line

CPI_BY_YEAR = {
    2019: 255.657,
    2020: 258.811,
    2021: 270.970,
    2022: 292.655,
    2023: 304.702,
    2024: 313.689,
}


def deflate_to_2019(value, year):
    """Convert nominal `value` in `year` dollars to constant 2019 dollars."""
    if value is None:
        return None
    return value * CPI_BY_YEAR[2019] / CPI_BY_YEAR[year]


def score_wealth(tenure, housing_value, age, year):
    """Strong / Adequate / Below using housing tenure + value as wealth proxy.

    Rules (frozen 2019 thresholds; housing values CPI-deflated to 2019$):
      - Owner outright (TEN=2): Strong if any value, Adequate if no VALP signal
      - Owner with mortgage (TEN=1) AND VALP_2019 ≥ Strong: Strong
      - Owner with mortgage AND VALP_2019 ≥ Adequate: Adequate
      - Owner with mortgage AND VALP_2019 < Adequate: Below
      - Renter (TEN=3) AND age ≥ 35: Below
      - Renter AND age < 35: Adequate (life-stage allowance for younger renters)
      - Occupied without rent (TEN=4): Below (insecurity proxy)
    """
    if tenure is None:
        return None
    valp_2019 = deflate_to_2019(housing_value, year) if housing_value else None
    if tenure == 2:  # owned free and clear
        return "strong"
    if tenure == 1:  # owned with mortgage
        if valp_2019 is None:
            return "adequate"
        if valp_2019 >= VALP_STRONG_2019:
            return "strong"
        if valp_2019 >= VALP_ADEQUATE_2019:
            return "adequate"
        return "below"
    if tenure == 3:  # rented
        if age is not None and age < 35:
            return "adequate"
        return "below"
    if tenure == 4:  # occupied without rent
        return "below"
    return None


# ---------------------------------------------------------------------------
# Family: life-stage thresholds by age band.
#
# PUMS variables used: AGEP, MAR, RELSHIPP (relationship), HHT (household type).
# We don't have community/social-isolation directly; we proxy via household
# structure (NP > 1 OR partnered marital status = "stable").
#
# A 20-year-old single college-aged is fine. A 40-year-old married with kids is
# Strong. A 70-year-old widowed who lives independently in a stable household
# is Strong (the spec example).
#
# MAR codes (PUMS):
#   1 = Married
#   2 = Widowed
#   3 = Divorced
#   4 = Separated
#   5 = Never married or under 15
# ---------------------------------------------------------------------------


def score_family(age, mar, hh_size):
    """Strong / Adequate / Below by age-band life-stage.

    Conventions:
      - "stable household" proxy: hh_size > 1 OR person is widowed/married
        (i.e., not isolated never-married-and-living-alone past midlife).
      - Under 18: not scored (FLY is for the producing population). Returns None.
    """
    if age is None or age < 18:
        return None  # children are not scored on family domain in v0.8
    if mar is None:
        return None

    is_partnered = mar == 1  # currently married
    was_partnered_stable = mar in (1, 2)  # married or widowed
    living_alone = (hh_size == 1)

    if 18 <= age < 30:
        # Single is fine; isolation would be living alone never-married, which
        # is uncommon at this age. Strong if stable HH, Adequate if alone.
        if not living_alone:
            return "strong"
        return "adequate"

    if 30 <= age < 45:
        # Family-formation prime years. Married/partnered is Strong; stable
        # single household is Adequate; isolated and never-partnered is Below.
        if is_partnered:
            return "strong"
        if not living_alone:
            return "adequate"
        return "below"

    if 45 <= age < 65:
        # Mid-life: married/partnered or widowed (stable trajectory) is Strong.
        # Divorced or separated but in stable HH is Adequate. Living alone
        # never-married is Below.
        if is_partnered:
            return "strong"
        if was_partnered_stable:
            return "strong" if not living_alone else "adequate"
        if not living_alone:
            return "adequate"
        return "below"

    # 65+: widowed-and-stable or married is Strong; stable single is Adequate;
    # isolated alone is Below (the loneliness-of-old-age problem the spec
    # acknowledges).
    if is_partnered or was_partnered_stable:
        if not living_alone or mar == 2:  # widowed living alone is acceptable
            return "strong"
        return "adequate"
    if not living_alone:
        return "adequate"
    return "below"


# ---------------------------------------------------------------------------
# Health: county-level life expectancy assigned to person, downgraded by
# disability.
#
# v0.8 simplification: use STATE-level life expectancy (NCHS NVSS state tables)
# rather than county-level for the PUMS person score. PUMS gives ST + PUMA but
# not county; county allocation requires a PUMA→county crosswalk that's a
# separate piece of work. State-level LE is a reasonable v1 floor.
#
# DIS = 1 means person reports disability → downgrade one tier.
# ---------------------------------------------------------------------------

# 2019 state life expectancy at birth, both sexes, all races, NCHS NVSS.
# Source: CDC/NCHS National Vital Statistics Reports Vol 70 No 12 (2022), with
# some interpolation across the most recent state-level publication. This is a
# v0.8 fixture; refresh when NCHS publishes annual state estimates.
STATE_LIFE_EXPECTANCY_2019 = {
    "01": 75.5, "02": 78.7, "04": 79.4, "05": 75.7, "06": 81.1,
    "08": 80.4, "09": 80.7, "10": 78.6, "11": 78.7, "12": 80.0,
    "13": 77.6, "15": 81.7, "16": 79.8, "17": 79.6, "18": 77.9,
    "19": 79.8, "20": 79.0, "21": 75.9, "22": 75.5, "23": 79.4,
    "24": 79.0, "25": 80.5, "26": 78.4, "27": 80.7, "28": 74.6,
    "29": 77.5, "30": 78.6, "31": 79.4, "32": 78.6, "33": 79.6,
    "34": 80.7, "35": 78.5, "36": 81.1, "37": 78.0, "38": 79.3,
    "39": 77.0, "40": 75.9, "41": 79.6, "42": 78.7, "44": 79.7,
    "45": 76.7, "46": 79.0, "47": 75.7, "48": 79.0, "49": 80.5,
    "50": 79.7, "51": 79.2, "53": 80.2, "54": 74.4, "55": 79.7,
    "56": 78.0,
}

LE_STRONG_THRESHOLD = 78.0  # ≥ 2019 national life expectancy
LE_ADEQUATE_THRESHOLD = 75.0  # 75 ≤ LE < 78


def score_health(state_fips, disabled):
    """Strong / Adequate / Below by state life expectancy, then DIS downgrade."""
    if state_fips is None:
        return None
    le = STATE_LIFE_EXPECTANCY_2019.get(state_fips)
    if le is None:
        return None
    if le >= LE_STRONG_THRESHOLD:
        tier = "strong"
    elif le >= LE_ADEQUATE_THRESHOLD:
        tier = "adequate"
    else:
        tier = "below"
    if disabled:
        # Downgrade one tier: strong → adequate, adequate → below, below → below.
        if tier == "strong":
            return "adequate"
        if tier == "adequate":
            return "below"
        return "below"
    return tier


# ---------------------------------------------------------------------------
# Education: attainment × employment alignment.
#
# PUMS variables: SCHL (educational attainment), ESR (employment status).
#
# SCHL codes (PUMS): 1–15 < HS, 16 = regular HS, 17 = GED, 18–19 some college,
# 20 = associate, 21 = bachelor, 22 = master, 23 = professional, 24 = doctoral.
# ESR codes: 1 employed, 2 employed-not-at-work, 3 unemployed, 4–5 armed
# forces, 6 not in labor force.
# ---------------------------------------------------------------------------


def score_education(schl, esr, age):
    """Strong / Adequate / Below for education + employment alignment.

    Strong: Bachelor's+ AND working, OR HS+ AND retired with full attainment.
    Adequate: HS+ AND working, OR some college, OR Bachelor's+ not working
              (student, caregiver, stay-at-home).
    Below: under HS, OR long-term not-in-labor-force pre-retirement age.
    """
    if schl is None or esr is None or age is None:
        return None

    is_employed = esr in (1, 2, 4, 5)  # employed civilian or armed forces
    is_retired_age = age >= 65

    has_hs = schl >= 16
    has_some_college = schl >= 18
    has_bachelors = schl >= 21

    # Strong: BA+ working, or BA+ retired (stayed credentialed through career).
    if has_bachelors and (is_employed or is_retired_age):
        return "strong"
    # Adequate: HS+ working, or some college (any status), or BA+ not working
    # (student / caregiver / between jobs).
    if has_hs and is_employed:
        return "adequate"
    if has_some_college:
        return "adequate"
    if has_bachelors:
        return "adequate"
    if has_hs and is_retired_age:
        return "adequate"
    if has_hs:
        # HS but not employed and not retired age — long-term not-in-LF.
        return "below"
    # Under-HS:
    if is_retired_age:
        return "adequate"
    return "below"


# ---------------------------------------------------------------------------
# Convert a 5-tuple of tier strings into a person FLY value in [0, 1].
# Domains that score None contribute 0 (no credit). Documented as
# "missing-data-equals-Below" — a defensible-but-pessimistic default.
# ---------------------------------------------------------------------------

DOMAINS = ("purchasing_power", "wealth", "family", "health", "education")
TIER_VALUE = {"strong": 0.20, "adequate": 0.10, "below": 0.0, None: 0.0}


def person_fly_from_tiers(tiers):
    """Sum of TIER_VALUE across the 5 domains. Range 0.0–1.0."""
    return sum(TIER_VALUE[t] for t in tiers.values())
