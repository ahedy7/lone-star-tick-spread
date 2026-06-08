"""Central configuration for the lone star tick range-expansion pipeline.

Everything that another stage might want to tweak (taxa, country, paths, date
cutoffs, CRS, GBIF field whitelist) lives here so the rest of the code never
hardcodes magic strings. Import as:

    from config import (PROCESSED_DIR, GBIF_FIELDS, ...)

Nothing in this module performs I/O or hits the network at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env (gitignored) so GBIF credentials are available via os.getenv below.
# Done at import so every entry point (scripts, notebook) picks them up.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # python-dotenv optional; env vars may be set externally
    pass

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# src/config.py -> repo root is one level up from this file.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
INTERIM_DIR: Path = DATA_DIR / "interim"
PROCESSED_DIR: Path = DATA_DIR / "processed"

REPORTS_DIR: Path = PROJECT_ROOT / "reports"
FIGURES_DIR: Path = REPORTS_DIR / "figures"

# Local cache for GBIF download artifacts / API responses (gitignored).
CACHE_DIR: Path = PROJECT_ROOT / ".cache"

for _d in (RAW_DIR, INTERIM_DIR, PROCESSED_DIR, FIGURES_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
# Primary target: the lone star tick. We resolve the GBIF backbone taxonKey at
# runtime (see acquire_gbif.resolve_taxon_key) rather than hardcoding it, so the
# pipeline stays correct if GBIF re-keys the backbone.
PRIMARY_SPECIES_NAME: str = "Amblyomma americanum"
PRIMARY_RANK: str = "SPECIES"

# Background / observation-effort denominator for later bias correction.
#
# Taxon choice: family Ixodidae ("hard ticks"). Rationale -- the lone star tick
# is a hard tick, and Ixodidae records share the same collectors, the same
# field/clinical sampling protocols, and the same iNaturalist "ticks" search
# behaviour. So Ixodidae effort is a tight proxy for "where were people in a
# position to observe/record a lone star tick". Broader denominators (all
# Arachnida, all arthropods) dilute the effort signal with taxa observed by
# different communities under different protocols. This is flagged as an open
# decision in the Stage 1 report.
BACKGROUND_FAMILY_NAME: str = "Ixodidae"
BACKGROUND_RANK: str = "FAMILY"

# --------------------------------------------------------------------------- #
# Geographic / temporal scope
# --------------------------------------------------------------------------- #
COUNTRY_CODE: str = "US"  # GBIF ISO-3166 alpha-2

# CRS policy: keep raw pulls in WGS84. Reprojection to CONUS Albers happens in a
# later analysis stage -- NOT here.
RAW_CRS: str = "EPSG:4326"        # WGS84 lon/lat, as delivered by GBIF
ANALYSIS_CRS: str = "EPSG:5070"   # NAD83 / Conus Albers, equal-area (later stage)

# Known temporal facts (documented in the README too):
#  - CDC "established" status is sticky/monotonic -> cumulative footprint.
#  - The dense, mappable citizen-science signal is roughly 2015 -> present.
DENSE_SIGNAL_START_YEAR: int = 2015
CDC_ESTABLISHED_THROUGH_YEAR: int = 2024

# --------------------------------------------------------------------------- #
# GBIF pull settings
# --------------------------------------------------------------------------- #
# Shared occurrence filters applied to every GBIF pull in Stage 1.
GBIF_BASE_FILTERS: dict[str, object] = {
    "hasCoordinate": True,        # georeferenced only
    "hasGeospatialIssue": False,  # drop records GBIF flags as geospatially broken
    "country": COUNTRY_CODE,
}

# Minimum columns we must retain from every occurrence pull.
GBIF_FIELDS: list[str] = [
    "decimalLatitude",
    "decimalLongitude",
    "eventDate",
    "year",
    "coordinateUncertaintyInMeters",
    "basisOfRecord",
    "datasetKey",
    "occurrenceID",
    "institutionCode",
]

# Public search-API fallback is fetched sequentially over one keep-alive
# connection (concurrency triggers GBIF connection drops / pool stalls).
# This is only the connection-pool size hint for that single session.
SEARCH_WORKERS: int = 2
# Polite pause between sequential search-API page requests (seconds).
SEARCH_PAGE_DELAY_SECONDS: float = 0.2
# (connect, read) timeout for search pages -- short read so a throttled/hung
# page fails fast and we can persist a partial sample instead of stalling.
SEARCH_TIMEOUT: tuple[int, int] = (10, 15)
# Retry budget for search pages (kept low for fast failure on throttle).
SEARCH_RETRY_TOTAL: int = 2
# Hard wall-clock bound per page (seconds). GBIF can trickle bytes when
# throttling, defeating the inactivity-based read timeout; this guarantees a
# stuck page is abandoned so the pull stops and writes a partial sample.
SEARCH_PAGE_HARD_TIMEOUT: int = 25

# GBIF credentials are read from the environment (.env). Absent -> download API
# is unavailable and callers should fall back / report a blocker.
GBIF_USER: str | None = os.getenv("GBIF_USER")
GBIF_PWD: str | None = os.getenv("GBIF_PWD")
GBIF_EMAIL: str | None = os.getenv("GBIF_EMAIL")

# --------------------------------------------------------------------------- #
# Stage 2 -- cleaning + harmonization
# --------------------------------------------------------------------------- #
# Stage 1 wrote the extracted SIMPLE_CSV occurrence tables to data/interim/.
# These are the inputs Stage 2 cleans. Tab-separated, as GBIF delivers them.
# (Filenames carry the pull date + record count; update if a fresh pull lands.)
STAGE2_INPUTS: dict[str, str] = {
    "target": "gbif_amblyomma_americanum_us_2026-06-03_n38916.csv",
    "background": "gbif_ixodidae_background_us_2026-06-04_n411644.csv",
}
INTERIM_SEP: str = "\t"  # GBIF SIMPLE_CSV is tab-separated

# Columns we standardize/keep from the rich SIMPLE_CSV. Everything else is
# dropped at the end. (We read a few extra raw columns -- issue, day, etc. --
# during cleaning, then trim to this analysis-ready set.)
STAGE2_KEEP_COLUMNS: list[str] = [
    "gbifID",
    "occurrenceID",
    "datasetKey",
    "publishingOrgKey",
    "institutionCode",
    "basisOfRecord",
    "species",
    "scientificName",
    "stateProvince",
    "countryCode",
    "decimalLatitude",
    "decimalLongitude",
    "coordinateUncertaintyInMeters",
    "x_5070",
    "y_5070",
    "eventDate",
    "year",
    "month",
    "source_type",
    "coordinate_rounded",
    "hex_reliable",
    "h3_cell",
    "in_dense_window",
]

# --- Source classification ------------------------------------------------- #
# The critical Stage 2 step: keep NEON (systematic), iNaturalist (opportunistic
# citizen science) and other (museum/preserved) SEPARABLE.
#
# Classification is driven primarily by GBIF publishingOrganizationKey -- the
# most robust signal, since institutionCode is often blank. Verified against the
# GBIF registry API during Stage 2:
#   NEON org  e794e60e-... publishes BOTH NEON occurrence datasets:
#       922feca7-...  "NEON Biorepository Tick Collection" (PRESERVED_SPECIMEN)
#       12315bb8-...  "NEON ticks sampled using drag cloths" (SAMPLING_EVENT)
#   IMPORTANT: the ~14.6k blank-institutionCode A. americanum records are the
#   NEON drag-cloth SAMPLING_EVENT dataset (12315bb8-...), NOT iNaturalist.
#   iNat org  28eb1a3f-... publishes 50c9509d-... "iNaturalist Research-grade".
NEON_PUBLISHING_ORG_KEY: str = "e794e60e-e558-4549-99f8-cfb241cdce24"
INATURALIST_PUBLISHING_ORG_KEY: str = "28eb1a3f-1c15-4a95-931a-4af90ecb574d"
INATURALIST_RG_DATASET_KEY: str = "50c9509d-22c7-4a22-a47d-8c48425ef4a7"
NEON_DATASET_KEYS: frozenset[str] = frozenset(
    {
        "922feca7-a681-4f61-89a7-9ac8fe968d82",  # Biorepository specimens
        "12315bb8-8ab3-446a-b5a4-2be93aade242",  # drag-cloth sampling events
    }
)
# Source labels (use everywhere so the strings never drift).
SRC_NEON: str = "NEON"
SRC_INAT: str = "iNaturalist"
SRC_OTHER: str = "other"

# --- Coordinate quality ---------------------------------------------------- #
# Hex-reliability cutoff -- NOT a data-quality reject. Records coarser than this
# cannot be placed confidently in a res-5 hex (~17 km across), so they are
# flagged hex_reliable=False and EXCLUDED from the fine hex density surface, but
# they are KEPT in the processed output: at the county level (~CDC validation /
# leading-edge frontier in Stage 3) they remain usable, and a looser county
# bound will be applied there. Records with NO stated uncertainty stay reliable.
# DECISION (user, Stage 2): 10 km. ~937 target / ~4,266 background get flagged.
MAX_COORDINATE_UNCERTAINTY_M: float = 10_000.0

# GBIF interpreted issue flags that indicate a genuinely broken coordinate ->
# drop. (Note: Stage 1 pulled with hasGeospatialIssue=false, so GBIF already
# removed its "geospatial issue" severity class; this rule is belt-and-braces
# and documents intent. Expect ~0 removals on the current pulls.)
DROP_COORDINATE_ISSUES: frozenset[str] = frozenset(
    {
        "ZERO_COORDINATE",
        "COORDINATE_INVALID",
        "COORDINATE_OUT_OF_RANGE",
        "COUNTRY_COORDINATE_MISMATCH",
        "PRESUMED_NEGATED_LATITUDE",
        "PRESUMED_NEGATED_LONGITUDE",
        "PRESUMED_SWAPPED_COORDINATE",
        "COORDINATE_REPROJECTION_FAILED",
        "COORDINATE_REPROJECTION_SUSPICIOUS",
    }
)
# Issue flags we only FLAG (keep the record, set a boolean column). Rounding to
# ~3 decimals (~100 m) is fine relative to res-5 H3 cells.
FLAG_COORDINATE_ISSUES: frozenset[str] = frozenset({"COORDINATE_ROUNDED"})

# --- Deduplication --------------------------------------------------------- #
# DECISION/default -- see Stage 2 report. We deliberately do NOT dedup on
# (coords + date) alone: NEON drag-cloth sampling emits many legitimately
# distinct records at the same fixed-site coordinates on the same day, so a
# coords+date key would collapse ~75-90% of records and destroy the systematic
# effort signal. Instead:
#   1. drop exact duplicate occurrenceID (re-published identical records),
#   2. drop cross-source coordinate/date collisions only (an observation that
#      reached GBIF through >1 source), preserving within-source replication.
DEDUP_COORD_DECIMALS: int = 5  # ~1.1 m rounding for the collision key
DEDUP_CROSS_SOURCE_ONLY: bool = True

# --- Spatial binning ------------------------------------------------------- #
# H3 hexagonal cell resolution. DECISION/default -- see Stage 2 report. Res 5 ~=
# 252 km^2 / ~8.5 km edge, a reasonable county-ish grain for range mapping.
H3_RESOLUTION: int = 5

# --------------------------------------------------------------------------- #
# CDC validation layer
# --------------------------------------------------------------------------- #
# CDC county-level establishment dataset for Amblyomma americanum. The project
# brief referenced "established counties through 2024"; as of this build CDC has
# updated the published map/data to "through 2025" (an .xlsx, not a .csv). We
# pull whatever the surveillance page currently links and record the actual
# vintage in the manifest. The species page is the canonical landing page; the
# direct data URL is auto-discovered in acquire_cdc.py.
CDC_LANDING_PAGE: str = (
    "https://www.cdc.gov/ticks/data-research/facts-stats/"
    "lone-star-tick-surveillance.html"
)

# Raw CDC establishment workbook (Stage 1 output in data/raw/) and the sheet to
# read. Stage 2 harmonizes county FIPS to 5-digit strings for clean joins later.
CDC_RAW_XLSX: str = "cdc_amblyomma_americanum_established_2026-06-03.xlsx"
CDC_SHEET: str = "A. americanum Records 2025"
# Status values in the workbook -> we treat "Established" as the canonical
# established footprint; "Reported" / "No records" are kept but flagged false.
CDC_ESTABLISHED_STATUS_VALUE: str = "Established"

# --------------------------------------------------------------------------- #
# Stage 2 -- processed (analysis-ready) outputs
# --------------------------------------------------------------------------- #
# Regenerable. Written to data/processed/; raw/ and interim/ are never touched.
PROCESSED_TARGET_FILE: str = "amblyomma_americanum_clean.parquet"
PROCESSED_BACKGROUND_FILE: str = "ixodidae_background_clean.parquet"
PROCESSED_CDC_FILE: str = "cdc_amblyomma_americanum_established_tidy.csv"
PROCESSED_LEDGER_FILE: str = "stage2_cleaning_ledger.json"

# --------------------------------------------------------------------------- #
# Stage 3 -- effort correction + stabilization
# --------------------------------------------------------------------------- #
# --- Rolling time windows -------------------------------------------------- #
# Configurable rolling windows over the dense window (2015 -> latest). Default:
# 3-year windows stepped by 1 year (2015-2017, 2016-2018, ...). iNaturalist is
# thin per cell per single year, so 3-year windows stabilize each frame while
# still showing motion. Pre-2015 records are kept aside for static historical
# context only (in_dense_window=False) and never enter the windowed analysis.
# DECISION/default -- surfaced in the Stage 3 report.
WINDOW_LENGTH_YEARS: int = 3
WINDOW_STEP_YEARS: int = 1
WINDOW_START_YEAR: int = DENSE_SIGNAL_START_YEAR  # 2015
# Latest window-end year. None -> derive from the data's max observed year so
# the pipeline tracks fresh pulls automatically (logged at runtime).
WINDOW_END_YEAR: int | None = None

# --- Empirical-Bayes shrinkage (beta-binomial) ----------------------------- #
# Thin cells have tiny, noisy denominators. We fit a global beta(alpha, beta)
# prior from the pooled cell-window proportions (MLE of the beta-binomial
# marginal, MoM fallback) and report the posterior mean per cell-window as the
# stabilized effort-corrected intensity:  shrunk = (num + a) / (den + a + b).
# Data-rich cells keep their signal; thin cells shrink toward the global rate.
# A minimum denominator can be required for a cell-window to enter the prior fit
# (0 = use every cell-window with den > 0).
SHRINKAGE_MIN_DENOMINATOR_FOR_PRIOR: int = 0

# --- Frontier metrics ------------------------------------------------------ #
# Northern range limit: a high-percentile latitude of "positive" cells, NOT the
# single northernmost outlier point. 0.95 = the 95th-percentile latitude.
# DECISION/default -- surfaced in the report.
NORTHERN_LIMIT_PERCENTILE: float = 0.95
# Corrected surface: a cell-window counts as "occupied" when its stabilized
# effort-corrected intensity (shrunk_ratio) is at or above this share. The
# global pooled lone-star share of iNaturalist tick effort is ~0.20, so 0.10
# means "lone star was at least ~10% of tick-spotting effort here".
# DECISION/default -- surfaced in the report.
OCCUPIED_INTENSITY_THRESHOLD: float = 0.10
# Raw-count surface: a cell-window counts as "detected" when it holds at least
# this many raw iNaturalist A. americanum observations (effort-uncorrected).
RAW_MIN_COUNT: int = 1

# --- County detection table ------------------------------------------------ #
# County assignment tolerates more positional error than res-5 hex assignment,
# so hex_reliable==False iNaturalist records are rescued here under a looser
# bound. A record enters the county table if its stated coordinate uncertainty
# is <= this many metres (records with no stated uncertainty are kept).
# DECISION/default -- surfaced in the report.
COUNTY_MAX_UNCERTAINTY_M: float = 50_000.0
# Sources that contribute detections to the county table (iNaturalist + NEON).
COUNTY_DETECTION_SOURCES: tuple[str, ...] = (SRC_INAT, SRC_NEON)
# US county polygons (5-digit FIPS) for the point-in-county spatial join.
# Fetched once and cached under .cache/ (never written to raw/ or interim/).
COUNTY_GEOJSON_URL: str = (
    "https://raw.githubusercontent.com/plotly/datasets/master/"
    "geojson-counties-fips.json"
)
COUNTY_GEOJSON_CACHE: str = "us_counties_fips.geojson"

# --- Stage 3 processed outputs (regenerable) -------------------------------- #
STAGE3_EFFORT_CELLS_FILE: str = "effort_corrected_cells.parquet"
STAGE3_NEON_PRESENCE_FILE: str = "neon_presence.parquet"
STAGE3_FRONTIER_METRICS_FILE: str = "frontier_metrics.csv"
STAGE3_COUNTY_DETECTIONS_FILE: str = "county_detections.parquet"
STAGE3_PRIOR_FILE: str = "stage3_beta_prior.json"

# --------------------------------------------------------------------------- #
# Stage 5 -- CDC validation (a spatial checkpoint, NOT a temporal validation)
# --------------------------------------------------------------------------- #
# Reads ONLY data/processed/ (the Stage 3 county_detections table + the Stage 2
# tidy CDC table) and writes data/processed/ + reports/figures/ + viz/. Nothing
# here touches data/raw/ or data/interim/.
#
# Framing baked into every caption: CDC "established" is a single cumulative,
# sticky snapshot, so this compares our *cumulative ever-detected* footprint to
# the official footprint -- a spatial checkpoint. Leading-edge counties are
# candidates to watch, not confirmed expansion; blind spots reflect OUR observer
# coverage, not absence of ticks.

# --- Detection bar (mirrors CDC's establishment threshold) ----------------- #
# A county counts as "detected" only if it clears a small observation threshold,
# so a single stray does not register. The county_detections table carries
# aggregated counts (life stage is collapsed at Stage 3's county aggregation, so
# the brief's "multiple life stages where NEON provides them" refinement is not
# recoverable here); the bar therefore reduces to an observation count: a county
# is detected in a window when its total iNat+NEON A. americanum count >= this.
# DECISION/default: 2 ("more than one observation").
STAGE5_DETECTION_MIN_OBS: int = 2
# Comparison footprint:
#   "cumulative" -- ever-detected: a county clears the bar in ANY rolling window
#                   (the union across windows). CDC established is itself a
#                   cumulative sticky snapshot, so this is the apples-to-apples
#                   footprint and the MAIN comparison.
#   "recent"     -- only the most recent window clears the bar.
# DECISION/default: "cumulative".
STAGE5_COMPARISON_MODE: str = "cumulative"
# How to treat CDC "Reported" counties (literature/anecdotal records, NOT
# established). We never fold them into the established footprint -- the
# establishment bar is the yardstick for recall/precision -- but a detected
# "Reported" county is *partial corroboration*, so it is split into its own
# leading-edge sub-bucket and also reported as a lenient-precision variant.
# DECISION/default: "separate".
STAGE5_REPORTED_TREATMENT: str = "separate"

# --- Category vocabulary (use everywhere so the strings never drift) -------- #
STAGE5_CAT_CONFIRMED: str = "confirmed"                  # detected & established
STAGE5_CAT_LEADING_REPORTED: str = "leading_edge_reported"   # detected, CDC reported
STAGE5_CAT_LEADING_NORECORDS: str = "leading_edge_no_records"  # detected, CDC none
STAGE5_CAT_BLIND_SPOT: str = "blind_spot"               # established, not detected
STAGE5_CAT_NEITHER: str = "neither"                     # not detected, not established
# Human-readable labels for legends/captions.
STAGE5_CATEGORY_LABELS: dict[str, str] = {
    STAGE5_CAT_CONFIRMED: "Confirmed (detected & CDC-established)",
    STAGE5_CAT_LEADING_REPORTED: "Leading edge · CDC reported (partial corroboration)",
    STAGE5_CAT_LEADING_NORECORDS: "Leading edge · CDC no records (frontier candidate)",
    STAGE5_CAT_BLIND_SPOT: "Blind spot (CDC-established, we missed)",
    STAGE5_CAT_NEITHER: "Neither",
}
# Categorical palette for the choropleth + interactive layers (color-blind aware).
STAGE5_CATEGORY_COLORS: dict[str, str] = {
    STAGE5_CAT_CONFIRMED: "#2c7fb8",          # blue   -- agreement
    STAGE5_CAT_LEADING_REPORTED: "#fdae61",   # amber  -- partial corroboration
    STAGE5_CAT_LEADING_NORECORDS: "#d7191c",  # red    -- frontier candidate
    STAGE5_CAT_BLIND_SPOT: "#bdbdbd",         # grey   -- coverage gap
    STAGE5_CAT_NEITHER: "#f2f2f2",            # near-white background
}

# --- Stage 5 outputs -------------------------------------------------------- #
# Canonical deliverables (data/processed/, regenerable).
STAGE5_VALIDATION_PARQUET: str = "county_validation.parquet"
STAGE5_VALIDATION_CSV: str = "county_validation.csv"
STAGE5_METRICS_JSON: str = "stage5_validation_metrics.json"
# Validation choropleth (reports/figures/).
STAGE5_FIG_VALIDATION: str = "stage5_validation_choropleth.png"
# Web layer for the interactive map (viz/data/). The .geojson is the servable
# canonical asset; validation.js wraps the same FeatureCollection in a global so
# the page opens straight from file:// (browsers block fetch() of siblings under
# file://, a <script> tag does not). Only counties that are established OR
# detected are exported (the "neither" universe is left as the basemap).
STAGE5_WEB_GEOJSON: str = "county_validation.geojson"
STAGE5_WEB_JS: str = "validation.js"  # window.LST_VALIDATION = {type:FeatureCollection,...}
# Douglas-Peucker tolerance (degrees) applied to the exported county polygons to
# keep the file:// bundle small. ~0.01 deg ~ 1 km -- invisible at national zoom.
STAGE5_WEB_SIMPLIFY_TOLERANCE: float = 0.01

# --------------------------------------------------------------------------- #
# Stage 4 -- presentation-grade visualization
# --------------------------------------------------------------------------- #
# Reads ONLY data/processed/ Stage 3 outputs. Writes the interactive app to
# viz/ and polished static figures to reports/figures/. Every knob a designer
# might want to tweak lives here so the renderers never hardcode magic values.

# --- Output locations ------------------------------------------------------ #
VIZ_DIR: Path = PROJECT_ROOT / "viz"
VIZ_DATA_DIR: Path = VIZ_DIR / "data"
for _d in (VIZ_DIR, VIZ_DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Web data export (consumed by the deck.gl H3HexagonLayer). The compact JSON is
# the canonical deliverable; the .js bundle wraps the same payload in a global
# so the page opens straight from the filesystem (file://) with no local server
# -- browsers block fetch() of sibling files under file://, a <script> tag does
# not. Both carry identical data.
VIZ_CELLS_JSON: str = "cells.json"          # {meta, windows:{window:[{h,r,s}]}}
VIZ_NEON_JSON: str = "neon.json"            # {window:[{lat,lon,detected}]}
VIZ_FRONTIER_JSON: str = "frontier.json"    # per-window frontier metrics
VIZ_FRONTIER_LINES_JSON: str = "frontier_lines.json"  # {window:[[lon,lat],...]}
VIZ_BUNDLE_JS: str = "bundle.js"            # window.LST_DATA = {cells,neon,...}
# Small machine-readable data-vintage stamp the deployed map reads to show
# "Data vintage / Last updated", and that the monthly auto-refresh rewrites each
# run (Stage 6). The same fields are also embedded in the bundle meta, so the
# stamp still renders when the page is opened straight from file:// (no fetch).
VIZ_META_JSON: str = "meta.json"
# CDC establishment layer vintage. UNLIKE the GBIF/iNaturalist frontier (which
# the monthly workflow refreshes), the CDC footprint is an ANNUAL vintage that
# updates rarely and by hand. Surfaced in the map's scoping note so "self-
# updating" never implies the CDC layer is live. Matches CDC_SHEET above.
CDC_DATA_VINTAGE: str = "2025"
# Ratios are rounded before export to keep the payload tiny (~thousands of rows).
VIZ_RATIO_DECIMALS: int = 4

# --- Shared cartography knobs (used by BOTH static figures and interactive) - #
# Sequential, perceptually-uniform colormap suited to a rate. magma_r runs
# pale-cream (low share, recedes into the light CARTO Positron basemap) ->
# magenta -> near-black (high share, pops). Reversed magma keeps the
# perceptual uniformity of magma while putting "high" on the dark/saturated end
# where the eye expects intensity on a light base.
# DECISION/default -- surfaced in the Stage 4 report.
VIZ_COLORMAP: str = "magma_r"
# The colour scale is a RATE (share of tick observations that were lone star),
# so we fix a shared domain across windows and surfaces -- otherwise per-frame
# autoscaling would hide the very advance we are trying to show. Values above
# the top are clamped. ~0.6 covers the bulk of the corrected distribution while
# keeping mid-range contrast.
VIZ_COLOR_DOMAIN: tuple[float, float] = (0.0, 0.6)
# Number of colour stops sampled from the matplotlib colormap and handed to the
# JS renderer, so the interactive ramp is pixel-identical to the static figures.
VIZ_COLOR_STOPS: int = 12
# Plain-language unit shown on every legend / colourbar.
VIZ_RATE_LABEL: str = "share of tick observations that were lone star"

# --- Map framing ----------------------------------------------------------- #
# CONUS-east clip. The lone star tick signal is eastern US; a couple of coarse
# Alaska/Pacific iNaturalist cells exist in the table and would otherwise zoom
# the map out to nothing. These bounds frame the data, not reject it.
VIZ_LON_MIN: float = -106.0
VIZ_LON_MAX: float = -66.0
VIZ_LAT_MIN: float = 24.0
VIZ_LAT_MAX: float = 49.5

# --- Interactive (deck.gl) ------------------------------------------------- #
# CARTO Positron: clean light basemap, no API key/token required. Used for the
# interactive (maplibre style JSON) and the static figures (contextily tiles),
# so the whole project shares one basemap identity.
# DECISION/default -- surfaced in the Stage 4 report.
VIZ_BASEMAP_STYLE_URL: str = (
    "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
)
VIZ_INITIAL_LATITUDE: float = 37.5
VIZ_INITIAL_LONGITUDE: float = -86.0
VIZ_INITIAL_ZOOM: float = 4.1
# Default surface + window the app opens on. Corrected is the honest headline
# surface; the most recent window is the end of the story. None -> latest window.
VIZ_DEFAULT_SURFACE: str = "shrunk"   # "shrunk" (corrected) | "raw"
VIZ_DEFAULT_WINDOW: str | None = None
# Animation pacing: milliseconds the play loop dwells on each window.
# DECISION/default -- surfaced in the Stage 4 report.
VIZ_ANIM_MS_PER_WINDOW: int = 900
VIZ_HEX_OPACITY: float = 0.78         # choropleth fill opacity over the basemap

# --- Insufficient-data (prior bleed-through) treatment --------------------- #
# In the CORRECTED surface the shrinkage floor (numerator + alpha) / (denominator
# + alpha + beta) can never reach zero, so a cell with NO lone star sightings
# (numerator == 0) but very few total tick sightings reads as a faint nonzero
# value (up to ~0.10) that is the prior leaking through, NOT real ticks. Such
# cells are given a neutral "too few sightings to judge" treatment instead of a
# faint color. A cell qualifies as prior-bleed when it has no lone star signal
# (numerator == 0) AND fewer than this many total tick sightings. Cells WITH any
# lone star signal (numerator >= 1) are always shown -- they are real detections,
# however thin -- so this never hides frontier signal, and the frontier metrics
# (which require shrunk_ratio >= OCCUPIED_INTENSITY_THRESHOLD, unreachable at
# numerator == 0) are unaffected.
# DECISION/default: 5 (tuned against the California prior-bleed specks).
VIZ_MIN_OBS_FOR_CORRECTED: int = 5
# Neutral fill for prior-bleed cells (light grey), and its opacity (kept low so
# the wash reads as "thin data" without competing with the magma signal).
VIZ_INSUFFICIENT_DATA_COLOR: str = "#cfcfcf"
VIZ_INSUFFICIENT_DATA_OPACITY: float = 0.45

# --- Static figures -------------------------------------------------------- #
VIZ_FIG_DPI: int = 220
# contextily tile provider key path (CartoDB.Positron) -- matches the
# interactive basemap. Fetched at render time; figures degrade to a styled
# state/land base (dissolved county polygons) if tiles are unreachable.
VIZ_STATIC_BASEMAP: str = "CartoDB.Positron"
# Net-advance annotation uses a flat degrees->km factor (1 deg lat ~= 111 km).
VIZ_KM_PER_DEG_LAT: float = 111.0
VIZ_FIG_RAW_VS_CORR: str = "stage4_raw_vs_corrected_recent.png"
VIZ_FIG_NORTHERN_LIMIT: str = "stage4_northern_limit.png"
VIZ_FIG_CENTROID: str = "stage4_centroid_latitude.png"
VIZ_GIF_CORRECTED: str = "stage4_frontier_corrected.gif"
VIZ_GIF_FPS: float = 1.4              # frames/sec for the exported animation

# --- Frontier-focused animation (the hero) --------------------------------- #
# At national scale the ~66 km corrected advance is a hair and the eye reads
# coverage fill-in as cells "spawning". Cropping to the frontier band makes the
# advance a meaningful fraction of the frame, and an explicit advancing edge
# line shows the motion honestly. Defaults frame the Midwest -> Northeast band.
# DECISION/default -- surfaced in the Stage 4 report.
VIZ_FRONTIER_LAT_MIN: float = 37.0
VIZ_FRONTIER_LAT_MAX: float = 44.0
VIZ_FRONTIER_LON_MIN: float = -95.0
VIZ_FRONTIER_LON_MAX: float = -68.0
VIZ_FRONTIER_BASEMAP_ZOOM: int = 6
# Northern-edge method:
#   "per_longitude" -- an organic front that climbs: at each longitude sample we
#                      take a HIGH-PERCENTILE latitude of positive cells pooled
#                      over a rolling longitude window. Using a percentile (not
#                      the literal northernmost cell) makes the edge robust to a
#                      single far-north sighting, so it can't imply a larger
#                      shift than the data shows -- it is the per-longitude
#                      analogue of the 95th-pct northern-limit metric.
#   "percentile"    -- a single horizontal line at the 95th-pct latitude of all
#                      positive cells (matches the northern-limit chart exactly).
# DECISION/default -- surfaced in the Stage 4 report.
VIZ_FRONTIER_METHOD: str = "per_longitude"
VIZ_FRONTIER_LON_STEP: float = 1.0        # spacing of edge sample points (deg)
VIZ_FRONTIER_LON_HALFWIDTH: float = 1.75  # rolling pool half-width (deg)
VIZ_FRONTIER_PERCENTILE: float = NORTHERN_LIMIT_PERCENTILE  # 0.95, robust edge
VIZ_FRONTIER_SMOOTH_BINS: int = 3         # moving-average window in samples (1=off)
VIZ_FRONTIER_MIN_BIN_CELLS: int = 4       # positive cells needed in a pool
# Positive cell = corrected (shrunk) surface at/above the Stage 3 occupied
# threshold (config.OCCUPIED_INTENSITY_THRESHOLD), so the edge matches the
# frontier table. Ring the cells that newly cross positive north of the prior
# window's edge, so genuine new northern detections pop.
VIZ_FRONTIER_HIGHLIGHT_NEW: bool = True
VIZ_FRONTIER_HIGHLIGHT_COLOR: str = "#00d2ff"  # cyan ring -- "new this window"
VIZ_GIF_FRONTIER: str = "stage4_frontier_advance.gif"
VIZ_GIF_FRONTIER_FPS: float = 1.2
# Also (re)build the supplementary national-context GIF (stage4_frontier_*).
# RECOMMENDATION: keep it, but label it supplementary in the README.
VIZ_KEEP_CONUS_GIF: bool = True

# --------------------------------------------------------------------------- #
# HTTP etiquette
# --------------------------------------------------------------------------- #
HTTP_TIMEOUT_SECONDS: int = 60
HTTP_MAX_RETRIES: int = 5
HTTP_BACKOFF_FACTOR: float = 1.5  # exponential backoff base
USER_AGENT: str = (
    "lone-star-tick-spread/0.1 (portfolio research pipeline; "
    "+https://github.com/)"
)
