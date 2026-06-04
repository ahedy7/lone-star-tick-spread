# Lone Star Tick Range Expansion

A reproducible pipeline + interactive time-slider map of the northward range
expansion of the **lone star tick** (*Amblyomma americanum*) across the United
States. Occurrence data are corrected for citizen-science observation bias and
validated against CDC county-level surveillance.

> **Status: Stage 1 (data acquisition + sanity check) only.** Stages 2–5
> (cleaning/bias-correction, spread detection, CDC validation, interactive map,
> scheduled auto-refresh) are not built yet.

---

## Repository layout

```
lone-star-tick-spread/
├── data/
│   ├── raw/         # immutable pulls, stamped with date + record count + .manifest.json
│   ├── interim/     # extracted / cached intermediates (e.g. basemap, download extracts)
│   └── processed/   # analysis-ready outputs (later stages)
├── src/
│   ├── config.py        # all taxa, country, paths, date cutoffs, fields, CRS, HTTP etiquette
│   ├── acquire_gbif.py  # taxonKey resolution + GBIF download API (+ search-API fallback)
│   └── acquire_cdc.py   # CDC established-counties dataset download
├── notebooks/
│   └── 01_stage1_sanity.ipynb   # Stage 1 sanity report (counts, years, coords, basis, map)
├── reports/figures/             # saved figures
├── requirements.txt             # pinned dependencies
├── .env.example                 # GBIF credential template (copy to .env, never committed)
└── README.md
```

---

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

GBIF's **download API** (the full, citable occurrence pull) needs a free GBIF
account. Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env      # then edit GBIF_USER / GBIF_PWD / GBIF_EMAIL
```

`.env` is gitignored — **secrets are never committed.**

---

## Running Stage 1

```bash
# GBIF occurrences (lone star tick + Ixodidae background effort layer).
# With .env credentials -> full, DOI-citable download API.
# Without credentials   -> public search-API fallback (partial sample; see caveats).
python src/acquire_gbif.py            # both taxa
python src/acquire_gbif.py --only primary --method download

# CDC county-level establishment dataset (auto-discovers the current file).
python src/acquire_cdc.py

# Sanity report.
jupyter notebook notebooks/01_stage1_sanity.ipynb
```

Every raw pull lands in `data/raw/` unmodified, stamped with the pull date and
record count, alongside a `*.manifest.json` recording provenance (resolved
taxonKey, filters, method, GBIF download key/DOI, counts, completeness).

- **Taxon keys are resolved at runtime** from the GBIF backbone (never
  hardcoded). As of this build: *A. americanum* = `2184301`, Ixodidae = `9167`.
- **Coordinates are kept in EPSG:4326.** Reprojection to EPSG:5070 (CONUS
  Albers, equal-area) happens in a later analysis stage — not at acquisition.

---

## Things we already know (don't re-discover these)

- **CDC "established" status is sticky / monotonic.** Once a county is recorded
  as established, it stays established in all later years. The CDC layer is
  therefore a **cumulative footprint**, *not* an annual snapshot — keep this in
  mind when comparing it to year-by-year occurrence spread.
- **The dense, mappable signal is roughly 2015 → present.** Citizen-science
  reporting (largely iNaturalist via GBIF) ramps up sharply in the last decade,
  so the eventual time-lapse is **recent-weighted**; pre-2015 coverage is sparse
  and dominated by museum/specimen records.

---

## Data sources

- **GBIF** — occurrence records for *Amblyomma americanum* and family Ixodidae
  (US, georeferenced). Note: **iNaturalist research-grade observations flow into
  GBIF**, so a single GBIF pull captures both museum specimens *and*
  citizen-science sightings. Cite via the download DOI recorded in the manifest.
- **CDC** — [Lone Star Tick Surveillance](https://www.cdc.gov/ticks/data-research/facts-stats/lone-star-tick-surveillance.html),
  county-level establishment dataset (cumulative footprint).

## Related work

- **Hart, C.E., et al. (2022).** *Insects.* Used iNaturalist observations for
  tick distribution monitoring — direct precedent for leveraging
  citizen-science records (which reach us here via GBIF) to track tick range.
