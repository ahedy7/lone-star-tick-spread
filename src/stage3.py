"""Stage 3 -- effort correction + stabilization.

Turns the cleaned, source-tagged occurrence tables in ``data/processed/`` into
analysis-ready, *per-cell-per-window* layers that separate genuine range signal
from citizen-science observation effort. NOTHING here reads ``data/raw/`` or
``data/interim/`` -- only the Stage 2 processed parquets are consumed, and every
output is regenerable.

The four deliverables (all written to ``data/processed/``):

* ``effort_corrected_cells.parquet`` -- the core. Per h3_cell per rolling window:
  numerator (iNaturalist *A. americanum*), denominator (iNaturalist Ixodidae,
  i.e. all-tick), the raw effort ratio, and the empirical-Bayes *shrunk* ratio.
* ``neon_presence.parquet`` -- the independent structured layer: per h3_cell per
  window, a boolean "lone star detected" by NEON. NEON multiplicity is sampling
  intensity, NOT abundance, so counts never enter any ratio.
* ``frontier_metrics.csv`` -- per window, northern range limit / centroid
  latitude / occupied area, on BOTH the raw-count surface and the corrected
  (shrunk) surface, so the raw-vs-corrected contrast is a table.
* ``county_detections.parquet`` -- per county FIPS per window, detections from
  iNaturalist + NEON, additionally rescuing hex_reliable==False iNaturalist
  records under a looser county-level uncertainty bound. (No CDC join -- Stage 5.)

The effort ratio (the heart of the method)
-------------------------------------------
For each cell and window, on iNaturalist records with hex_reliable==True only::

    numerator   = # iNaturalist A. americanum observations
    denominator = # iNaturalist Ixodidae (all-tick) observations, same cell+window
    raw_ratio   = numerator / denominator

The denominator is the **iNaturalist subset** of the Ixodidae background, NOT the
full background (which is ~88% NEON). Numerator and denominator both come from the
same opportunistic process, so platform growth cancels in the ratio. NEON records
appear in neither. Because *A. americanum* is itself a hard tick, the cleaned
target records are an exact subset of the cleaned background, so we derive BOTH
the numerator and the denominator from the single background table -- this
guarantees numerator <= denominator and a valid proportion for the beta-binomial.

All tunables (window length/step, percentile, thresholds, county bound) live in
config.py.

Run as a script to (re)generate every Stage 3 output:
    python src/stage3.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config

try:
    import h3
except ImportError as exc:  # pragma: no cover - dependency is pinned
    raise ImportError(
        "h3 is required for Stage 3 spatial work. `pip install h3`."
    ) from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,
)
log = logging.getLogger("stage3")

TARGET_SPECIES = config.PRIMARY_SPECIES_NAME  # "Amblyomma americanum"


# --------------------------------------------------------------------------- #
# Load (processed only)
# --------------------------------------------------------------------------- #
def load_processed() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the Stage 2 background + target parquets from data/processed/."""
    bpath = config.PROCESSED_DIR / config.PROCESSED_BACKGROUND_FILE
    tpath = config.PROCESSED_DIR / config.PROCESSED_TARGET_FILE
    for p in (bpath, tpath):
        if not p.exists():
            raise FileNotFoundError(
                f"Processed input not found: {p}. Run Stage 2 (`python "
                "src/clean.py`) first."
            )
    background = pd.read_parquet(bpath)
    target = pd.read_parquet(tpath)
    log.info(
        "Loaded processed: background=%d rows, target=%d rows",
        len(background), len(target),
    )
    return background, target


# --------------------------------------------------------------------------- #
# Task 1 -- rolling time windows
# --------------------------------------------------------------------------- #
def build_windows(max_year: int) -> pd.DataFrame:
    """Configurable rolling windows over the dense window.

    Returns a frame with columns: window (label), window_start, window_end.
    Default: 3-year windows stepped by 1 year, 2015-2017, 2016-2018, ...
    """
    end_year = config.WINDOW_END_YEAR if config.WINDOW_END_YEAR is not None else max_year
    length = config.WINDOW_LENGTH_YEARS
    step = config.WINDOW_STEP_YEARS
    starts = list(range(config.WINDOW_START_YEAR, end_year - length + 2, step))
    rows = [
        {"window": f"{s}-{s + length - 1}", "window_start": s,
         "window_end": s + length - 1}
        for s in starts
    ]
    win = pd.DataFrame(rows)
    log.info(
        "Built %d rolling windows (length=%dyr, step=%dyr): %s",
        len(win), length, step, ", ".join(win["window"]),
    )
    return win


def _explode_to_windows(df: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Replicate each record into every window whose [start, end] covers its year.

    Overlapping windows share records by design (a 2017 record sits in
    2015-2017, 2016-2018 and 2017-2019); each window is later aggregated
    independently so platform growth cannot leak across frames.
    """
    yr = df["year"].to_numpy()
    pieces = []
    for w in windows.itertuples(index=False):
        mask = (yr >= w.window_start) & (yr <= w.window_end)
        if not mask.any():
            continue
        sub = df.loc[mask].copy()
        sub["window"] = w.window
        sub["window_start"] = w.window_start
        sub["window_end"] = w.window_end
        pieces.append(sub)
    if not pieces:
        return df.iloc[0:0].assign(window="", window_start=0, window_end=0)
    return pd.concat(pieces, ignore_index=True)


# --------------------------------------------------------------------------- #
# Cell geometry helper
# --------------------------------------------------------------------------- #
def _cell_centroids(cells: pd.Series) -> pd.DataFrame:
    """Map each unique h3_cell to its centroid (lat, lon)."""
    uniq = pd.Index(cells.unique())
    latlng = [h3.cell_to_latlng(c) for c in uniq]
    return pd.DataFrame(
        {"h3_cell": uniq,
         "cell_lat": [ll[0] for ll in latlng],
         "cell_lon": [ll[1] for ll in latlng]}
    )


# --------------------------------------------------------------------------- #
# Task 2 -- NEON presence (independent structured layer; NOT in the ratio)
# --------------------------------------------------------------------------- #
def build_neon_presence(
    background: pd.DataFrame, windows: pd.DataFrame
) -> pd.DataFrame:
    """Collapse the NEON stream to presence per h3_cell per window.

    For every cell NEON sampled in a window we emit a boolean lone_star_detected
    (any NEON *A. americanum* record there). n_neon_records is sampling
    INTENSITY only -- it is reported for transparency and never fed into a ratio.
    """
    neon = background[
        (background["source_type"] == config.SRC_NEON)
        & (background["in_dense_window"])
    ].copy()
    neon = _explode_to_windows(neon, windows)
    neon["is_target"] = neon["species"].astype("string").eq(TARGET_SPECIES)

    grp = neon.groupby(["window", "window_start", "window_end", "h3_cell"])
    out = grp.agg(
        n_neon_records=("is_target", "size"),
        lone_star_detected=("is_target", "any"),
    ).reset_index()

    out = out.merge(_cell_centroids(out["h3_cell"]), on="h3_cell", how="left")
    out = out.sort_values(["window_start", "h3_cell"]).reset_index(drop=True)
    log.info(
        "NEON presence: %d cell-windows | lone-star-positive=%d | "
        "NEON-sampled cells (unique)=%d",
        len(out), int(out["lone_star_detected"].sum()), out["h3_cell"].nunique(),
    )
    return out


# --------------------------------------------------------------------------- #
# Task 3 -- effort-corrected ratio (iNaturalist only, hex_reliable only)
# --------------------------------------------------------------------------- #
def build_effort_cells(
    background: pd.DataFrame, windows: pd.DataFrame
) -> pd.DataFrame:
    """Per h3_cell per window: numerator, denominator, raw_ratio.

    Universe = iNaturalist records with hex_reliable==True in the dense window.
    Denominator = all such Ixodidae (all-tick) records in the cell+window;
    numerator = the *A. americanum* subset of exactly those records. Both come
    from the same opportunistic stream, so the ratio is an effort-corrected
    share in [0, 1].
    """
    inat = background[
        (background["source_type"] == config.SRC_INAT)
        & (background["in_dense_window"])
        & (background["hex_reliable"])
    ].copy()
    inat["is_target"] = inat["species"].astype("string").eq(TARGET_SPECIES)
    inat = _explode_to_windows(inat, windows)

    grp = inat.groupby(["window", "window_start", "window_end", "h3_cell"])
    cells = grp.agg(
        denominator=("is_target", "size"),
        numerator=("is_target", "sum"),
    ).reset_index()
    cells["numerator"] = cells["numerator"].astype(int)
    cells["denominator"] = cells["denominator"].astype(int)
    cells["raw_ratio"] = cells["numerator"] / cells["denominator"]

    cells = cells.merge(_cell_centroids(cells["h3_cell"]), on="h3_cell", how="left")
    cells = cells.sort_values(["window_start", "h3_cell"]).reset_index(drop=True)
    log.info(
        "Effort cells: %d cell-windows across %d unique cells | "
        "numerator total=%d, denominator total=%d, pooled rate=%.4f",
        len(cells), cells["h3_cell"].nunique(),
        int(cells["numerator"].sum()), int(cells["denominator"].sum()),
        cells["numerator"].sum() / max(cells["denominator"].sum(), 1),
    )
    return cells


# --------------------------------------------------------------------------- #
# Task 4 -- empirical-Bayes beta-binomial shrinkage
# --------------------------------------------------------------------------- #
def fit_beta_prior(numerator: np.ndarray, denominator: np.ndarray) -> dict[str, Any]:
    """Fit a global beta(alpha, beta) prior to the pooled cell-window counts.

    Primary: MLE of the beta-binomial marginal likelihood (scipy). Fallback:
    method of moments on the per-cell-window proportions. Returns alpha, beta,
    the implied prior mean and the fit method used.
    """
    k = np.asarray(numerator, dtype=float)
    n = np.asarray(denominator, dtype=float)
    keep = n >= max(config.SHRINKAGE_MIN_DENOMINATOR_FOR_PRIOR, 1)
    k, n = k[keep], n[keep]
    if len(k) == 0:
        return {"alpha": 1.0, "beta": 1.0, "prior_mean": 0.5,
                "method": "degenerate-uniform", "n_cellwindows": 0}

    pooled_rate = float(k.sum() / n.sum())
    p = k / n
    m, v = float(p.mean()), float(p.var())

    # Method-of-moments seed (also the fallback).
    if 0.0 < m < 1.0 and v > 0.0:
        kappa = max(m * (1.0 - m) / v - 1.0, 1e-3)
        a_mom, b_mom = m * kappa, (1.0 - m) * kappa
    else:  # degenerate proportions -> weak prior centred on the pooled rate.
        a_mom = max(pooled_rate, 1e-6) * 2.0
        b_mom = max(1.0 - pooled_rate, 1e-6) * 2.0

    method = "method-of-moments"
    alpha, beta = a_mom, b_mom
    try:
        from scipy.optimize import minimize
        from scipy.special import betaln

        def _nll(log_params: np.ndarray) -> float:
            a, b = np.exp(log_params)
            # Drop the binomial coefficient (constant in a, b).
            return float(-(betaln(k + a, n - k + b) - betaln(a, b)).sum())

        res = minimize(
            _nll, np.log([a_mom, b_mom]), method="Nelder-Mead",
            options={"xatol": 1e-5, "fatol": 1e-5, "maxiter": 20000},
        )
        if res.success and np.all(np.isfinite(res.x)):
            alpha, beta = (float(x) for x in np.exp(res.x))
            method = "beta-binomial-mle"
        else:  # pragma: no cover - optimiser rarely fails on this data
            log.warning("Beta-binomial MLE did not converge; using MoM prior.")
    except ImportError:  # pragma: no cover - scipy is pinned
        log.warning("scipy unavailable; using method-of-moments prior.")

    prior = {
        "alpha": alpha, "beta": beta,
        "prior_mean": alpha / (alpha + beta),
        "prior_strength": alpha + beta,
        "pooled_rate": pooled_rate,
        "method": method,
        "n_cellwindows": int(len(k)),
    }
    log.info(
        "Beta prior (%s): alpha=%.4f beta=%.4f -> mean=%.4f, strength=%.2f "
        "(pooled rate=%.4f, %d cell-windows)",
        method, alpha, beta, prior["prior_mean"], prior["prior_strength"],
        pooled_rate, len(k),
    )
    return prior


def apply_shrinkage(cells: pd.DataFrame, prior: dict[str, Any]) -> pd.DataFrame:
    """Add the posterior-mean shrunk_ratio = (num + a) / (den + a + b)."""
    cells = cells.copy()
    a, b = prior["alpha"], prior["beta"]
    cells["shrunk_ratio"] = (cells["numerator"] + a) / (
        cells["denominator"] + a + b
    )
    cells["shrinkage_delta"] = cells["shrunk_ratio"] - cells["raw_ratio"]
    return cells


# --------------------------------------------------------------------------- #
# Task 5 -- frontier metrics per window (raw counts vs corrected surface)
# --------------------------------------------------------------------------- #
def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    w = weights.sum()
    return float((values * weights).sum() / w) if w > 0 else float("nan")


def _percentile_lat(lats: np.ndarray, pct: float) -> float:
    return float(np.percentile(lats, pct * 100.0)) if len(lats) else float("nan")


def compute_frontier_metrics(cells: pd.DataFrame) -> pd.DataFrame:
    """Per window, robust frontier metrics on BOTH surfaces.

    RAW surface       -- effort-uncorrected iNaturalist *A. americanum* counts.
                         "detected" cell = numerator >= RAW_MIN_COUNT.
                         centroid weighted by numerator.
    CORRECTED surface -- the stabilized shrunk effort ratio.
                         "occupied" cell = shrunk_ratio >= OCCUPIED_INTENSITY_THRESHOLD.
                         centroid weighted by shrunk_ratio.

    Northern limit = the NORTHERN_LIMIT_PERCENTILE-th percentile latitude of the
    positive cells (robust to a single northern outlier), on each surface.
    """
    pct = config.NORTHERN_LIMIT_PERCENTILE
    thr = config.OCCUPIED_INTENSITY_THRESHOLD
    min_ct = config.RAW_MIN_COUNT
    cell_area = h3.average_hexagon_area(config.H3_RESOLUTION, unit="km^2")

    rows = []
    for win, g in cells.groupby("window"):
        lat = g["cell_lat"].to_numpy()
        num = g["numerator"].to_numpy(dtype=float)
        shr = g["shrunk_ratio"].to_numpy(dtype=float)

        raw_pos = num >= min_ct
        cor_pos = shr >= thr

        rows.append({
            "window": win,
            "window_start": int(g["window_start"].iloc[0]),
            "window_end": int(g["window_end"].iloc[0]),
            "n_cells": int(len(g)),
            # --- raw-count surface ---
            "raw_northern_limit_lat": _percentile_lat(lat[raw_pos], pct),
            "raw_centroid_lat": _weighted_mean(lat, num),
            "raw_occupied_cells": int(raw_pos.sum()),
            "raw_occupied_area_km2": float(raw_pos.sum() * cell_area),
            # --- corrected (shrunk) surface ---
            "corrected_northern_limit_lat": _percentile_lat(lat[cor_pos], pct),
            "corrected_centroid_lat": _weighted_mean(lat, shr),
            "corrected_occupied_cells": int(cor_pos.sum()),
            "corrected_occupied_area_km2": float(cor_pos.sum() * cell_area),
        })

    out = pd.DataFrame(rows).sort_values("window_start").reset_index(drop=True)
    log.info(
        "Frontier metrics: %d windows | percentile=%.2f, occupied thr=%.2f, "
        "raw min count=%d, cell area=%.1f km^2",
        len(out), pct, thr, min_ct, cell_area,
    )
    return out


# --------------------------------------------------------------------------- #
# Task 6 -- county detection table (iNat + NEON; rescues coarse iNat records)
# --------------------------------------------------------------------------- #
def _load_county_polygons() -> "Any":
    """Load US county polygons (5-digit FIPS), caching the GeoJSON under .cache/.

    Never writes to data/raw/ or data/interim/.
    """
    import geopandas as gpd

    cache = config.CACHE_DIR / config.COUNTY_GEOJSON_CACHE
    if cache.exists():
        counties = gpd.read_file(cache)
        log.info("Loaded county polygons from cache (%d features)", len(counties))
    else:
        log.info("Fetching county polygons -> %s", config.COUNTY_GEOJSON_URL)
        counties = gpd.read_file(config.COUNTY_GEOJSON_URL)
        try:
            counties.to_file(cache, driver="GeoJSON")  # cache for offline reruns
        except Exception as exc:  # pragma: no cover - caching is best-effort
            log.warning("Could not cache county polygons: %s", exc)
        log.info("Fetched county polygons (%d features)", len(counties))

    # Derive a clean 5-digit FIPS. The plotly file carries it as the feature id
    # and inside GEO_ID ("0500000US01001"); STATE+COUNTY is the robust fallback.
    if "id" in counties.columns and counties["id"].notna().all():
        counties["county_fips"] = counties["id"].astype(str).str.zfill(5)
    elif "GEO_ID" in counties.columns:
        counties["county_fips"] = counties["GEO_ID"].astype(str).str[-5:]
    else:
        counties["county_fips"] = (
            counties["STATE"].astype(str).str.zfill(2)
            + counties["COUNTY"].astype(str).str.zfill(3)
        )
    if counties.crs is None:
        counties = counties.set_crs(config.RAW_CRS)
    return counties[["county_fips", "geometry"]]


def build_county_detections(
    background: pd.DataFrame, windows: pd.DataFrame
) -> pd.DataFrame:
    """Per county FIPS per window: A. americanum detections from iNat + NEON.

    Includes hex_reliable==False iNaturalist records under a looser county-level
    uncertainty bound (county assignment tolerates more positional error than
    res-5 hex assignment). NaN uncertainty is kept. No CDC join (that is Stage 5).
    """
    import geopandas as gpd

    det = background[
        (background["source_type"].isin(config.COUNTY_DETECTION_SOURCES))
        & (background["in_dense_window"])
        & (background["species"].astype("string").eq(TARGET_SPECIES))
    ].copy()

    # Looser county-level uncertainty bound (NaN uncertainty kept).
    unc = det["coordinateUncertaintyInMeters"]
    within_county_bound = (unc <= config.COUNTY_MAX_UNCERTAINTY_M) | unc.isna()
    n_before = len(det)
    det = det[within_county_bound]
    n_rescued = int(
        (~det["hex_reliable"]).sum()
    )
    log.info(
        "County detections: %d/%d A. americanum iNat+NEON records within %.0f km "
        "county bound (%d of them hex_reliable==False, rescued from the hex surface)",
        len(det), n_before, config.COUNTY_MAX_UNCERTAINTY_M, n_rescued,
    )

    # Point-in-county spatial join (WGS84).
    counties = _load_county_polygons()
    pts = gpd.GeoDataFrame(
        det,
        geometry=gpd.points_from_xy(det["decimalLongitude"], det["decimalLatitude"]),
        crs=config.RAW_CRS,
    )
    joined = gpd.sjoin(pts, counties, how="left", predicate="within")
    n_unmatched = int(joined["county_fips"].isna().sum())
    if n_unmatched:
        log.info(
            "  %d detections fell outside all county polygons (offshore / "
            "border slivers); dropped from county table", n_unmatched,
        )
    joined = joined[joined["county_fips"].notna()].copy()

    joined = _explode_to_windows(
        pd.DataFrame(joined.drop(columns="geometry")), windows
    )
    joined["is_neon"] = joined["source_type"].eq(config.SRC_NEON)
    joined["is_inat"] = joined["source_type"].eq(config.SRC_INAT)

    grp = joined.groupby(["window", "window_start", "window_end", "county_fips"])
    out = grp.agg(
        n_total=("is_neon", "size"),
        n_neon=("is_neon", "sum"),
        n_inat=("is_inat", "sum"),
    ).reset_index()
    out["n_neon"] = out["n_neon"].astype(int)
    out["n_inat"] = out["n_inat"].astype(int)
    out["neon_detected"] = out["n_neon"] > 0
    out["inat_detected"] = out["n_inat"] > 0
    out["detected"] = out["n_total"] > 0
    out = out.sort_values(["window_start", "county_fips"]).reset_index(drop=True)
    log.info(
        "County detections: %d county-windows across %d unique counties",
        len(out), out["county_fips"].nunique(),
    )
    return out


# --------------------------------------------------------------------------- #
# Top-level runner
# --------------------------------------------------------------------------- #
def run_stage3(write: bool = True) -> dict[str, Any]:
    """Build every Stage 3 layer, optionally writing to data/processed/."""
    background, _target = load_processed()

    max_year = int(
        background.loc[background["in_dense_window"], "year"].max()
    )
    windows = build_windows(max_year)

    neon_presence = build_neon_presence(background, windows)
    cells = build_effort_cells(background, windows)
    prior = fit_beta_prior(
        cells["numerator"].to_numpy(), cells["denominator"].to_numpy()
    )
    cells = apply_shrinkage(cells, prior)
    frontier = compute_frontier_metrics(cells)
    county = build_county_detections(background, windows)

    if write:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        cells_cols = [
            "h3_cell", "window", "window_start", "window_end",
            "numerator", "denominator", "raw_ratio", "shrunk_ratio",
            "shrinkage_delta", "cell_lat", "cell_lon",
        ]
        cells[cells_cols].to_parquet(
            config.PROCESSED_DIR / config.STAGE3_EFFORT_CELLS_FILE, index=False
        )
        neon_presence.to_parquet(
            config.PROCESSED_DIR / config.STAGE3_NEON_PRESENCE_FILE, index=False
        )
        frontier.to_csv(
            config.PROCESSED_DIR / config.STAGE3_FRONTIER_METRICS_FILE,
            index=False, encoding="utf-8",
        )
        county.to_parquet(
            config.PROCESSED_DIR / config.STAGE3_COUNTY_DETECTIONS_FILE, index=False
        )
        (config.PROCESSED_DIR / config.STAGE3_PRIOR_FILE).write_text(
            json.dumps(prior, indent=2), encoding="utf-8"
        )
        log.info("Wrote Stage 3 outputs -> %s", config.PROCESSED_DIR)

    return {
        "windows": windows,
        "neon_presence": neon_presence,
        "effort_cells": cells,
        "prior": prior,
        "frontier": frontier,
        "county_detections": county,
    }


def main() -> int:
    out = run_stage3(write=True)
    log.info("Stage 3 complete.")
    f = out["frontier"]
    log.info("Frontier (corrected northern limit / centroid by window):")
    for r in f.itertuples(index=False):
        log.info(
            "  %s | raw N-limit=%.2f corr N-limit=%.2f | raw cent=%.2f corr cent=%.2f"
            " | raw occ=%d corr occ=%d",
            r.window, r.raw_northern_limit_lat, r.corrected_northern_limit_lat,
            r.raw_centroid_lat, r.corrected_centroid_lat,
            r.raw_occupied_cells, r.corrected_occupied_cells,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
