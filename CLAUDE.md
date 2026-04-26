# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single-file static landing page for **The Flourishing Life Year Index** — a marketing/methodology site for a county-level measure of the American good life, benchmarked against federal data. The page pitches the framework, shows example visualizations, and points to a (not-yet-implemented) `/assessment` flow.

The entire site is `index.html`. There is no build system, no test suite, no JavaScript, and no package dependencies. The `package-lock.json` is an empty stub (no `package.json`, no installed packages).

## Working in this codebase

- **Run locally**: open `index.html` directly in a browser, or serve it with `python3 -m http.server` from the repo root.
- **No build step.** Edits to `index.html` are the deploy artifact.
- **No JS.** All interactivity is CSS (`scroll-behavior`, animations, `:hover`). All charts are hand-written inline SVG with hardcoded coordinates and value labels — when you change a number, you must recompute the bar geometry (the comments above each `<g class="bar">` show the math, e.g. `y = 110 - (index * 0.649)`).
- **CSS lives in one `<style>` block** at lines ~10–1056. The design system is driven by CSS variables in `:root` (lines ~11–25): `--ink`, `--paper*`, `--accent`, `--rule`, and the five `--domain-N` colors. Reuse these rather than introducing new hex values.

## Page structure

`index.html` is organized as numbered sections (`§ 01` through `§ 04`), each with a matching CSS block above and HTML below:

1. Hero + national-stats strip (lines ~1078–1138)
2. `§ 01 The Unit` — mission (lines ~1141–1159)
3. `§ 02 The Arithmetic` — equation explanation (lines ~1162–1226)
4. `§ 03 The Framework` — five domain cards with inline-SVG charts (lines ~1229–1540). The five domains (purchasing power, wealth, family, health, education) are the conceptual core; each has its own `--domain-N` color.
5. `§ 04 How It Works` — methodology steps (lines ~1543–1587)
6. Quote + closing CTA + footer (lines ~1590–1644)

Anchor IDs (`#mission`, `#domains`, `#method`) are referenced from the nav and footer — preserve them when restructuring.

## Editorial voice

The copy is deliberately understated and methodology-forward (e.g. "Directional honesty over false precision", "If you can't reproduce our number, it's a bug"). The footer marks the project as `v0.3 Methodology Draft` and "Currently in development" — match this register when adding copy. Several CTAs link to `/assessment`, which doesn't exist yet; don't remove these links without confirming.
