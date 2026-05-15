// Extracted scoring engine from assessment.html for profile testing.
// Run: node soak_score_profiles.js

const fs = require("fs");
const curves = JSON.parse(fs.readFileSync("national_age_curves.json", "utf8"));

const MIDDLE_CLASS_FLOOR_USD = 48962;
const DEGREE_HAS_ASSOC = { none:0,hs:0,some_college:0,associate:1,bachelor:1,master:1,professional:1,doctorate:1 };
const DEGREE_HAS_BACHELOR = { none:0,hs:0,some_college:0,associate:0,bachelor:1,master:1,professional:1,doctorate:1 };

function ageBucketContaining(buckets, age) {
  for (const b of buckets) { if (age >= b.min_age && age <= b.max_age) return b; }
  if (age < buckets[0].min_age) return buckets[0];
  return buckets[buckets.length - 1];
}

function readPurchasingPower(curves, income, age) {
  const cohort = ageBucketContaining(curves.domains.purchasing_power.buckets, age);
  const median = cohort.value;
  const fmt$ = v => "$" + Math.round(v).toLocaleString();
  const floor = MIDDLE_CLASS_FLOOR_USD;
  const aboveFloor = income >= floor;
  const nearFloor  = !aboveFloor && income >= floor * 0.90;
  const wellAbove  = income >= floor * 1.5;
  let status, badge, reading;
  if (wellAbove) {
    status = "strong"; badge = "strong";
    reading = `${fmt$(income)} is well above floor $${floor.toLocaleString()} (cohort median ${fmt$(median)})`;
  } else if (aboveFloor) {
    status = "adequate"; badge = "adequate";
    reading = `${fmt$(income)} clears floor $${floor.toLocaleString()} (cohort median ${fmt$(median)})`;
  } else if (nearFloor) {
    status = "below"; badge = "below";
    reading = `${fmt$(income)} is just below floor $${floor.toLocaleString()} (within 10%)`;
  } else {
    status = "below"; badge = "below";
    reading = `${fmt$(income)} is below floor $${floor.toLocaleString()} (cohort median ${fmt$(median)})`;
  }
  return { status, badge, reading };
}

function readWealth(ownsHome, homeValue, hasInvestment, savingsFlow) {
  const owns = ownsHome === "yes";
  const inv  = hasInvestment === "yes";
  const flow = savingsFlow === "yes";
  const aboveMedianHome = (owns && homeValue != null && homeValue >= 340000);
  const proxyCount = (owns ? 1 : 0) + (aboveMedianHome ? 1 : 0) + (inv ? 1 : 0) + (flow ? 1 : 0);
  const strongCriterion = owns && flow && (aboveMedianHome || inv);
  let status, badge, reading;
  if (strongCriterion) {
    status = "strong"; badge = "strong";
    reading = `Owns + saves + (${aboveMedianHome ? "above-median home" : "investment income"}) → Q8.5 strong`;
  } else if (proxyCount >= 2) {
    status = "adequate"; badge = "adequate";
    reading = `${proxyCount}/4 proxies → adequate`;
  } else {
    status = "below"; badge = "below";
    reading = `${proxyCount}/4 proxies → below`;
  }
  return { status, badge, reading };
}

function readFamily(marital, hasKids, age) {
  const married = (marital === "married");
  const kids = (hasKids === "yes");
  if (age < 30) {
    let reading = age < 30 ? "Stage-neutral (below 30)" : "";
    if (married && kids) reading = "Married + kids (stage-neutral <30)";
    else if (married)    reading = "Married, no kids (stage-neutral <30)";
    else if (kids)       reading = "Kids, not married (stage-neutral <30)";
    else                 reading = "Single, no kids (stage-neutral <30)";
    return { status: "descriptive", badge: "descriptive", reading };
  }
  if (married) {
    return { status: "strong", badge: "strong", reading: kids ? "Married + kids at home → strong" : "Married, kids launched → strong (post-launch)" };
  }
  return { status: "below", badge: "below", reading: `${marital} past 30 → below Q8.6 criterion` };
}

function readHealth(selfRated) {
  if (selfRated === "excellent" || selfRated === "very_good") {
    return { status: "strong", badge: "strong", reading: `Health: ${selfRated} → strong` };
  }
  if (selfRated === "good") {
    return { status: "adequate", badge: "adequate", reading: "Health: good → adequate (modal answer)" };
  }
  return { status: "below", badge: "attention", reading: `Health: ${selfRated} → below` };
}

function readEducation(degree, skilledTrade, employmentMatch) {
  const hasBachelor = !!DEGREE_HAS_BACHELOR[degree];
  const hasAssociate = !!DEGREE_HAS_ASSOC[degree];
  const someCollege = degree === "some_college";
  const trade = skilledTrade === "yes";
  const usesCredential = employmentMatch === "yes";
  const employmentNA = employmentMatch === "na";
  const credentialStrong = hasBachelor || trade;
  const credentialAdequate = !credentialStrong && (hasAssociate || someCollege);

  if (employmentNA) {
    if (credentialStrong) return { status: "strong", badge: "strong", reading: "Strong credential + NA employment → strong" };
    if (credentialAdequate) return { status: "adequate", badge: "adequate", reading: "Adequate credential + NA employment → adequate" };
    return { status: "below", badge: "below", reading: "No credential + NA employment → below" };
  }
  if (credentialStrong && usesCredential) return { status: "strong", badge: "strong", reading: "Strong credential + uses it → strong" };
  if (credentialStrong && !usesCredential) return { status: "adequate", badge: "adequate", reading: "Strong credential + not using it → adequate" };
  if (credentialAdequate && usesCredential) return { status: "adequate", badge: "adequate", reading: "Adequate credential + uses it → adequate" };
  if (credentialAdequate && !usesCredential) return { status: "below", badge: "below", reading: "Adequate credential + not using → below" };
  return { status: "below", badge: "below", reading: "No credential → below" };
}

function scoreAssessment(inputs) {
  const reads = {
    purchasing_power: readPurchasingPower(curves, inputs.income, inputs.age),
    wealth:           readWealth(inputs.owns_home, inputs.home_value, inputs.investment_income, inputs.savings_flow),
    family:           readFamily(inputs.marital, inputs.kids, inputs.age),
    health:           readHealth(inputs.health),
    education:        readEducation(inputs.degree, inputs.skilled_trade, inputs.employment_match),
  };
  const evaluatable = ["purchasing_power", "wealth", "health", "education"];
  let strong = 0, adequate = 0, below = 0;
  for (const dom of evaluatable) {
    const s = reads[dom].status;
    if (s === "strong") strong++;
    else if (s === "adequate") adequate++;
    else if (s === "below") below++;
  }
  let tier, headline;
  if (strong >= 4)                        { tier = "great";   headline = "Doing great"; }
  else if (strong >= 3)                   { tier = "strong";  headline = "Strong overall"; }
  else if (strong + adequate >= 3)        { tier = "mixed";   headline = "Mixed picture"; }
  else                                     { tier = "gaps";    headline = "Significant gaps"; }
  return { reads, strong, adequate, below, tier, headline };
}

const profiles = [
  {
    name: "Recent grad (24M, $52k, rents, never-married, no kids, very-good health, bachelor, no trade, no invest)",
    inputs: { age: 24, sex: "M", income: 52000, owns_home: "no", home_value: null,
              marital: "never", kids: "no", health: "very_good",
              degree: "bachelor", skilled_trade: "no", employment_match: "yes",
              investment_income: "no", savings_flow: "no" }
  },
  {
    name: "Mid-career married homeowner (38F, $145k, owns $480k, married, kids, very-good, master's, no trade, invest)",
    inputs: { age: 38, sex: "F", income: 145000, owns_home: "yes", home_value: 480000,
              marital: "married", kids: "yes", health: "very_good",
              degree: "master", skilled_trade: "no", employment_match: "yes",
              investment_income: "yes", savings_flow: "yes" }
  },
  {
    name: "Single parent (32F, $48k, rents, separated, kids, good, some-college, no trade, no invest)",
    inputs: { age: 32, sex: "F", income: 48000, owns_home: "no", home_value: null,
              marital: "separated", kids: "yes", health: "good",
              degree: "some_college", skilled_trade: "no", employment_match: "yes",
              investment_income: "no", savings_flow: "no" }
  },
  {
    name: "Retired (70M, $62k, owns $310k, married, no kids, good, bachelor, no trade, invest)",
    inputs: { age: 70, sex: "M", income: 62000, owns_home: "yes", home_value: 310000,
              marital: "married", kids: "no", health: "good",
              degree: "bachelor", skilled_trade: "no", employment_match: "na",
              investment_income: "yes", savings_flow: "no" }
  },
  {
    name: "College student at home (20F, $185k parents' HH, rents/no, never-married, no kids, excellent, some-college, no trade, no invest)",
    inputs: { age: 20, sex: "F", income: 185000, owns_home: "no", home_value: null,
              marital: "never", kids: "no", health: "excellent",
              degree: "some_college", skilled_trade: "no", employment_match: "na",
              investment_income: "no", savings_flow: "no" }
  },
  {
    name: "Trade worker (35M, $78k, owns $260k, married, kids, good, hs, YES trade, no invest)",
    inputs: { age: 35, sex: "M", income: 78000, owns_home: "yes", home_value: 260000,
              marital: "married", kids: "yes", health: "good",
              degree: "hs", skilled_trade: "yes", employment_match: "yes",
              investment_income: "no", savings_flow: "no" }
  },
  {
    name: "Lower-income elderly (78F, $24k, rents, widowed, no kids, fair, hs, no trade, no invest)",
    inputs: { age: 78, sex: "F", income: 24000, owns_home: "no", home_value: null,
              marital: "widowed", kids: "no", health: "fair",
              degree: "hs", skilled_trade: "no", employment_match: "na",
              investment_income: "no", savings_flow: "no" }
  },
  {
    name: "High-earning urban single (31F, $215k, rents/Manhattan, never-married, no kids, excellent, professional, no trade, invest)",
    inputs: { age: 31, sex: "F", income: 215000, owns_home: "no", home_value: null,
              marital: "never", kids: "no", health: "excellent",
              degree: "professional", skilled_trade: "no", employment_match: "yes",
              investment_income: "yes", savings_flow: "yes" }
  },
];

for (const p of profiles) {
  const result = scoreAssessment(p.inputs);
  console.log(`\n=== ${p.name} ===`);
  console.log(`Tier: ${result.tier} | Headline: "${result.headline}" | ${result.strong}S ${result.adequate}A ${result.below}B`);
  for (const [dom, read] of Object.entries(result.reads)) {
    console.log(`  ${dom.padEnd(20)} [${read.badge.padEnd(11)}] ${read.reading}`);
  }
}
