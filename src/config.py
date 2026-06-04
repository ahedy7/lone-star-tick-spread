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
