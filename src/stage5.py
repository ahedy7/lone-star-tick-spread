"""Stage 5 -- CDC validation (a spatial checkpoint, not a temporal validation).

Compares our **cumulative ever-detected** lone star tick footprint (from the
Stage 3 ``county_detections`` table: iNaturalist + NEON, including coarse records
rescued at county precision) against the CDC county-level **established** status
(the Stage 2 tidy table). NOTHING here reads ``data/raw/`` or ``data/interim/`` --
only the two ``data/processed/`` inputs are consumed, and every output is
regenerable.

Honest framing (obeyed by every output/caption)
-----------------------------------------------
CDC "established" status is a single cumulative, **sticky** snapshot (the 2025
vintage), so this is a *spatial checkpoint*, not a temporal validation. We
compare our cumulative footprint to it:

* **confirmed** -- detected AND CDC-established (agreement);
* **leading edge** -- detected AND not established. Split into CDC *reported*
  (partial corroboration) vs CDC *no records* (a stronger frontier candidate).
  These are **candidates to watch, not confirmed expansion**;
* **blind spot** -- CDC-established AND not detected. These reflect **our
  observer coverage**, not absence of ticks.

The detection bar (Task 1)
--------------------------
A county counts as detected only if it clears a small observation threshold
(``config.STAGE5_DETECTION_MIN_OBS``, default 2 -> "more than one observation"),
mirroring CDC's establishment bar so a single stray does not register. The
cumulative ever-detected set is the **union over windows** of counties that clear
the bar in at least one rolling window (``config.STAGE5_COMPARISON_MODE`` =
``"cumulative"``; ``"recent"`` restricts to the most recent window).

Deliverables
------------
* ``data/processed/county_validation.(parquet|csv)`` -- per county: FIPS,
  detection status, CDC status, three-way category, plus supporting counts.
* ``data/processed/stage5_validation_metrics.json`` -- the confusion matrix,
  recall, precision (strict + lenient), and bucket counts, for transparency.
* ``reports/figures/stage5_validation_choropleth.png`` -- the three-category map.
* ``viz/data/county_validation.geojson`` + ``viz/data/validation.js`` -- the web
  layer powering the interactive CDC-established shading + leading-edge highlight.

All tunables live in ``config.py``. Run as a script to (re)generate everything:
    python src/stage5.py
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,
)
log = logging.getLogger("stage5")


# --------------------------------------------------------------------------- #
# Load (processed only)
# --------------------------------------------------------------------------- #
def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the Stage 3 county-detections parquet + the Stage 2 tidy CDC table."""
    det_path = config.PROCESSED_DIR / config.STAGE3_COUNTY_DETECTIONS_FILE
    cdc_path = config.PROCESSED_DIR / config.PROCESSED_CDC_FILE
    for p in (det_path, cdc_path):
        if not p.exists():
            raise FileNotFoundError(
                f"Stage 5 input missing: {p}. Run the earlier stages first "
                "(`python src/stage3.py` for detections, `python src/clean.py` "
                "for the tidy CDC table)."
            )
    detections = pd.read_parquet(det_path)
    # FIPS must stay a zero-padded 5-char string for a clean join.
    cdc = pd.read_csv(cdc_path, dtype={"county_fips": "string"})
    cdc["county_fips"] = cdc["county_fips"].str.zfill(5)
    detections["county_fips"] = (
        detections["county_fips"].astype("string").str.zfill(5)
    )
    if "established" in cdc.columns:
        cdc["established"] = cdc["established"].map(
            {True: True, False: False, "True": True, "False": False}
        ).astype("boolean").fillna(False).astype(bool)
    else:  # derive from status if the boolean column is absent
        cdc["established"] = cdc["status"].eq(config.CDC_ESTABLISHED_STATUS_VALUE)
    log.info(
        "Loaded inputs: %d county-windows (%d unique counties) | CDC table %d "
        "counties (%d established, %d reported, %d no-records)",
        len(detections), detections["county_fips"].nunique(), len(cdc),
        int(cdc["established"].sum()),
        int(cdc["status"].eq("Reported").sum()),
        int(cdc["status"].eq("No records").sum()),
    )
    return detections, cdc


# --------------------------------------------------------------------------- #
# Task 1 -- the detection set (robust "detected", cumulative ever-detected)
# --------------------------------------------------------------------------- #
def build_detection_summary(detections: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-county-window detections to a per-county detection summary.

    A county is "detected" in a window when its total iNat+NEON A. americanum
    count clears ``config.STAGE5_DETECTION_MIN_OBS`` (so a single stray does not
    register). The cumulative ever-detected flag is the union over windows; the
    "recent" mode restricts the flag to the most recent window. Either way the
    per-window bar is applied first, so the threshold always guards against a
    lone observation.
    """
    thr = config.STAGE5_DETECTION_MIN_OBS
    mode = config.STAGE5_COMPARISON_MODE
    windows = (
        detections[["window", "window_start"]]
        .drop_duplicates()
        .sort_values("window_start")["window"]
        .tolist()
    )
    latest_window = windows[-1]

    det = detections.copy()
    det["clears_bar"] = det["n_total"] >= thr

    grp = det.groupby("county_fips")
    summary = grp.agg(
        peak_window_obs=("n_total", "max"),
        n_windows_present=("window", "nunique"),
        n_inat_total=("n_inat", "sum"),
        n_neon_total=("n_neon", "sum"),
        ever_neon=("neon_detected", "any"),
    ).reset_index()

    # Windows in which the county clears the detection bar.
    cleared = det[det["clears_bar"]]
    cleared_grp = cleared.groupby("county_fips")
    cleared_summary = cleared_grp.agg(
        n_windows_detected=("window", "nunique"),
        first_detected_window=("window_start", "min"),
        last_detected_window=("window_start", "max"),
    ).reset_index()
    summary = summary.merge(cleared_summary, on="county_fips", how="left")
    summary["n_windows_detected"] = (
        summary["n_windows_detected"].fillna(0).astype(int)
    )

    detected_cumulative = summary["n_windows_detected"] > 0
    detected_recent = summary["county_fips"].isin(
        set(cleared.loc[cleared["window"] == latest_window, "county_fips"])
    )
    summary["detected_cumulative"] = detected_cumulative
    summary["detected_recent"] = detected_recent
    summary["detected"] = (
        detected_recent if mode == "recent" else detected_cumulative
    )

    # Tidy window labels for the first/last detected window (year -> "YYYY-YYYY").
    def _label(start: float) -> str | None:
        if pd.isna(start):
            return None
        s = int(start)
        return f"{s}-{s + config.WINDOW_LENGTH_YEARS - 1}"

    summary["first_detected_window"] = summary["first_detected_window"].map(_label)
    summary["last_detected_window"] = summary["last_detected_window"].map(_label)

    log.info(
        "Detection summary: %d counties with any detection; bar = n_total >= %d; "
        "mode = %s -> %d detected (cumulative=%d, recent=%d)",
        len(summary), thr, mode, int(summary["detected"].sum()),
        int(detected_cumulative.sum()), int(detected_recent.sum()),
    )
    return summary


# --------------------------------------------------------------------------- #
# Tasks 2 & 3 -- join to CDC by FIPS + three-way classification
# --------------------------------------------------------------------------- #
def classify(summary: pd.DataFrame, cdc: pd.DataFrame) -> pd.DataFrame:
    """Join detections to CDC status by 5-digit FIPS and classify each county.

    Universe = union of every CDC county and every detected county. A detected
    county absent from the CDC table is treated as CDC "No records" (and flagged
    ``in_cdc_table=False``). Categories follow ``config.STAGE5_CAT_*``.
    """
    cdc_cols = cdc[["county_fips", "state", "county", "status", "established"]].copy()
    cdc_cols = cdc_cols.rename(columns={"status": "cdc_status"})

    merged = summary.merge(cdc_cols, on="county_fips", how="outer")
    merged["in_cdc_table"] = merged["state"].notna()

    # Counties that exist only in the detection table get no CDC row -> "No
    # records"; counties only in CDC never detected -> detected=False, zero counts.
    merged["cdc_status"] = merged["cdc_status"].fillna("No records")
    merged["established"] = merged["established"].fillna(False).astype(bool)
    for col, fill in [
        ("detected", False), ("detected_cumulative", False),
        ("detected_recent", False), ("ever_neon", False),
    ]:
        merged[col] = merged[col].fillna(fill).astype(bool)
    for col in [
        "peak_window_obs", "n_windows_present", "n_windows_detected",
        "n_inat_total", "n_neon_total",
    ]:
        merged[col] = merged[col].fillna(0).astype(int)

    def _category(row: pd.Series) -> str:
        if row["detected"] and row["established"]:
            return config.STAGE5_CAT_CONFIRMED
        if row["detected"] and not row["established"]:
            if row["cdc_status"] == "Reported":
                return config.STAGE5_CAT_LEADING_REPORTED
            return config.STAGE5_CAT_LEADING_NORECORDS
        if (not row["detected"]) and row["established"]:
            return config.STAGE5_CAT_BLIND_SPOT
        return config.STAGE5_CAT_NEITHER

    merged["category"] = merged.apply(_category, axis=1)
    merged["category_label"] = merged["category"].map(config.STAGE5_CATEGORY_LABELS)

    n_outer_only_det = int((~merged["in_cdc_table"]).sum())
    if n_outer_only_det:
        log.info(
            "  %d detected counties were not in the CDC table -> treated as 'No "
            "records'", n_outer_only_det,
        )
    log.info(
        "Classification: %s",
        merged["category"].value_counts().to_dict(),
    )
    return merged.sort_values("county_fips").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Task 4 -- agreement metrics (confusion matrix, recall, precision)
# --------------------------------------------------------------------------- #
def compute_metrics(validation: pd.DataFrame) -> dict[str, Any]:
    """Confusion-matrix summary + recall/precision against CDC established.

    recall    = of CDC-established counties, the share we detected   = TP/(TP+FN).
    precision = of our detected counties, the share CDC confirms     = TP/(TP+FP).
    A *lenient* precision additionally credits detected CDC-"Reported" counties
    (partial corroboration). Counts of every bucket accompany the rates.
    """
    det = validation["detected"]
    est = validation["established"]
    rep = validation["cdc_status"].eq("Reported")

    tp = int((det & est).sum())                 # confirmed
    fp = int((det & ~est).sum())                # leading edge (all)
    fn = int((~det & est).sum())                # blind spot
    tn = int((~det & ~est).sum())               # neither
    fp_reported = int((det & ~est & rep).sum()) # leading edge, partial corroboration

    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    precision_lenient = (
        (tp + fp_reported) / (tp + fp) if (tp + fp) else float("nan")
    )

    counts = validation["category"].value_counts().to_dict()
    metrics = {
        "framing": (
            "Spatial checkpoint against the CDC 2025 cumulative established "
            "snapshot -- NOT a temporal validation. Leading-edge counties are "
            "candidates to watch; blind spots reflect our observer coverage."
        ),
        "detection_bar": {
            "min_obs": config.STAGE5_DETECTION_MIN_OBS,
            "comparison_mode": config.STAGE5_COMPARISON_MODE,
            "reported_treatment": config.STAGE5_REPORTED_TREATMENT,
        },
        "confusion_matrix": {
            "TP_confirmed": tp,
            "FP_leading_edge": fp,
            "FN_blind_spot": fn,
            "TN_neither": tn,
            "FP_leading_edge_reported": fp_reported,
            "FP_leading_edge_no_records": fp - fp_reported,
        },
        "recall_of_cdc_established": recall,
        "precision_strict": precision,
        "precision_lenient_incl_reported": precision_lenient,
        "n_detected": int(det.sum()),
        "n_cdc_established": int(est.sum()),
        "n_universe_counties": int(len(validation)),
        "category_counts": {
            k: int(counts.get(k, 0))
            for k in config.STAGE5_CATEGORY_LABELS
        },
    }
    log.info(
        "Metrics | recall=%.3f precision=%.3f (lenient=%.3f) | "
        "TP=%d FP=%d FN=%d TN=%d (FP: %d reported / %d no-records)",
        recall, precision, precision_lenient, tp, fp, fn, tn,
        fp_reported, fp - fp_reported,
    )
    return metrics


# --------------------------------------------------------------------------- #
# County polygons (cached; never writes raw/ or interim/)
# --------------------------------------------------------------------------- #
def _load_county_polygons() -> "Any":
    """Load US county polygons (5-digit FIPS) from the Stage 3 .cache/ GeoJSON.

    Reuses the same cached file Stage 3 fetched; falls back to fetching it if the
    cache is absent. Never writes to data/raw/ or data/interim/.
    """
    import geopandas as gpd

    cache = config.CACHE_DIR / config.COUNTY_GEOJSON_CACHE
    if cache.exists():
        counties = gpd.read_file(cache)
        log.info("Loaded county polygons from cache (%d features)", len(counties))
    else:
        log.info("Cache miss; fetching county polygons -> %s",
                 config.COUNTY_GEOJSON_URL)
        counties = gpd.read_file(config.COUNTY_GEOJSON_URL)
        try:
            counties.to_file(cache, driver="GeoJSON")
        except Exception as exc:  # pragma: no cover - caching is best-effort
            log.warning("Could not cache county polygons: %s", exc)

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


def attach_centroids(validation: pd.DataFrame, counties: "Any") -> pd.DataFrame:
    """Add a representative lat/lon per county (helps rank frontier candidates)."""
    rep = counties.copy()
    pts = rep.geometry.representative_point()
    rep["rep_lon"] = pts.x
    rep["rep_lat"] = pts.y
    out = validation.merge(
        rep[["county_fips", "rep_lat", "rep_lon"]], on="county_fips", how="left"
    )
    return out


# --------------------------------------------------------------------------- #
# Task 5 -- the validation choropleth
# --------------------------------------------------------------------------- #
def fig_validation_choropleth(
    validation: pd.DataFrame, counties: "Any", metrics: dict[str, Any]
) -> Path:
    """Three-category validation map over a CARTO Positron base.

    Confirmed (blue), leading edge split into reported (amber) / no-records
    (red), and blind spots (grey), with the "neither" universe left faint so the
    coloured signal reads. Title + caption obey the honest framing.
    """
    import matplotlib.pyplot as plt
    import matplotlib as mpl
    from matplotlib.patches import Patch

    # Reuse Stage 4's figure styling + basemap helpers for a consistent identity.
    import stage4

    stage4._apply_style()
    INK, MUTED = stage4.INK, stage4.MUTED

    gdf = counties.merge(
        validation[["county_fips", "category", "category_label"]],
        on="county_fips", how="left",
    )
    gdf["category"] = gdf["category"].fillna(config.STAGE5_CAT_NEITHER)
    # Frame to CONUS-east (same frame as the Stage 4 maps), in Web Mercator.
    gdf = gdf.to_crs(epsg=3857)
    minx, miny, maxx, maxy = stage4._frame_bounds_mercator()

    fig, ax = plt.subplots(figsize=(12.5, 9.2))
    fig.subplots_adjust(left=0.02, right=0.98, top=0.9, bottom=0.13)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_aspect("equal")
    if not stage4._add_basemap(ax, zoom=5):
        stage4._add_state_base(ax)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.grid(False)

    # Draw "neither" faint first, then the signal categories on top.
    order = [
        config.STAGE5_CAT_NEITHER,
        config.STAGE5_CAT_BLIND_SPOT,
        config.STAGE5_CAT_CONFIRMED,
        config.STAGE5_CAT_LEADING_REPORTED,
        config.STAGE5_CAT_LEADING_NORECORDS,
    ]
    for z, cat in enumerate(order):
        sub = gdf[gdf["category"] == cat]
        if sub.empty:
            continue
        is_neither = cat == config.STAGE5_CAT_NEITHER
        sub.plot(
            ax=ax,
            color=config.STAGE5_CATEGORY_COLORS[cat],
            edgecolor="white" if not is_neither else "none",
            linewidth=0.15 if not is_neither else 0.0,
            alpha=0.30 if is_neither else 0.88,
            zorder=2 + z,
        )

    # Legend with live bucket counts.
    counts = metrics["category_counts"]
    legend_cats = [
        config.STAGE5_CAT_CONFIRMED,
        config.STAGE5_CAT_LEADING_NORECORDS,
        config.STAGE5_CAT_LEADING_REPORTED,
        config.STAGE5_CAT_BLIND_SPOT,
    ]
    handles = [
        Patch(
            facecolor=config.STAGE5_CATEGORY_COLORS[c], edgecolor="white",
            label=f"{config.STAGE5_CATEGORY_LABELS[c]}  ({counts.get(c, 0)})",
        )
        for c in legend_cats
    ]
    ax.legend(
        handles=handles, loc="lower left", fontsize=9.5, framealpha=0.94,
        edgecolor="#cccccc", title="county classification",
        title_fontsize=10,
    ).set_zorder(10)

    rec = metrics["recall_of_cdc_established"]
    prec = metrics["precision_strict"]
    prec_len = metrics["precision_lenient_incl_reported"]
    ax.text(
        0.985, 0.045,
        f"recall {rec:.0%} · precision {prec:.0%}\n(lenient {prec_len:.0%})",
        transform=ax.transAxes, ha="right", va="bottom", fontsize=10.5,
        color=INK,
        bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#cccccc", alpha=0.92),
        zorder=10,
    )

    fig.suptitle(
        "Lone star tick: our footprint vs. the CDC established footprint",
        fontsize=17, fontweight="bold", color=INK, y=0.965,
    )
    fig.text(
        0.5, 0.085,
        "Spatial checkpoint against the CDC 2025 cumulative “established” "
        "snapshot — not a temporal validation. “Leading edge” "
        "counties (we detect, CDC has not established) are candidates to watch, "
        "not confirmed expansion; the no-records ones are the stronger frontier "
        "candidates. “Blind spots” (CDC-established, we miss) reflect "
        "our observer coverage, not absence of ticks. Detected = "
        f"≥{config.STAGE5_DETECTION_MIN_OBS} observations in some rolling "
        "window (cumulative across windows).",
        ha="center", va="top", fontsize=9.5, color=MUTED, wrap=True,
    )
    out = config.FIGURES_DIR / config.STAGE5_FIG_VALIDATION
    fig.savefig(out)
    plt.close(fig)
    log.info("Wrote figure -> %s", out)
    return out


# --------------------------------------------------------------------------- #
# Task 6 -- web layer (CDC-established shading + leading-edge highlight)
# --------------------------------------------------------------------------- #
def export_web_layer(validation: pd.DataFrame, counties: "Any") -> Path:
    """Export the interactive county layer: established + detected counties only.

    Writes ``viz/data/county_validation.geojson`` (servable) and
    ``viz/data/validation.js`` (``window.LST_VALIDATION = <FeatureCollection>;``
    so the app opens from file://). Polygons are lightly simplified to keep the
    bundle small. The "neither" universe is omitted -- it is left as the basemap.
    Each feature carries fips, category, cdc_status, detected, and counts so the
    app can shade/filter without a second lookup.
    """
    import geopandas as gpd

    keep = validation[validation["category"] != config.STAGE5_CAT_NEITHER]
    cols = [
        "county_fips", "category", "category_label", "cdc_status", "established",
        "detected", "peak_window_obs", "n_inat_total", "n_neon_total",
        "first_detected_window", "last_detected_window",
    ]
    gdf = counties.merge(keep[cols], on="county_fips", how="inner")
    if gdf.crs is None:
        gdf = gdf.set_crs(config.RAW_CRS)
    gdf = gdf.to_crs(config.RAW_CRS)  # WGS84 lon/lat for deck.gl
    # Simplify in WGS84 (tolerance is in degrees) to shrink the payload.
    gdf["geometry"] = gdf.geometry.simplify(
        config.STAGE5_WEB_SIMPLIFY_TOLERANCE, preserve_topology=True
    )
    # JSON-friendly dtypes.
    gdf["established"] = gdf["established"].astype(bool)
    gdf["detected"] = gdf["detected"].astype(bool)
    for c in ("peak_window_obs", "n_inat_total", "n_neon_total"):
        gdf[c] = gdf[c].astype(int)

    config.VIZ_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_geojson = config.VIZ_DATA_DIR / config.STAGE5_WEB_GEOJSON
    gdf.to_file(out_geojson, driver="GeoJSON")

    # Wrap the same FeatureCollection in a global for file:// loading. Also fold
    # the category palette in so the JS never re-hardcodes the colours.
    geo = json.loads(out_geojson.read_text(encoding="utf-8"))
    geo["meta"] = {
        "categoryColors": config.STAGE5_CATEGORY_COLORS,
        "categoryLabels": config.STAGE5_CATEGORY_LABELS,
        "detectionMinObs": config.STAGE5_DETECTION_MIN_OBS,
        "comparisonMode": config.STAGE5_COMPARISON_MODE,
    }
    out_js = config.VIZ_DATA_DIR / config.STAGE5_WEB_JS
    out_js.write_text(
        "window.LST_VALIDATION = " + json.dumps(geo, separators=(",", ":")) + ";",
        encoding="utf-8",
    )
    log.info(
        "Web layer: %d county features (established OR detected) -> %s (%.0f KB), "
        "%s (%.0f KB)",
        len(gdf), out_geojson.name, out_geojson.stat().st_size / 1024,
        out_js.name, out_js.stat().st_size / 1024,
    )
    return out_js


# --------------------------------------------------------------------------- #
# Reporting helper -- most interesting leading-edge counties
# --------------------------------------------------------------------------- #
def leading_edge_highlights(validation: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """The strongest frontier candidates (CDC no-records, then reported)."""
    le = validation[
        validation["category"].isin(
            [config.STAGE5_CAT_LEADING_NORECORDS,
             config.STAGE5_CAT_LEADING_REPORTED]
        )
    ].copy()
    le["_rank"] = le["category"].eq(config.STAGE5_CAT_LEADING_NORECORDS).astype(int)
    le = le.sort_values(
        ["_rank", "peak_window_obs", "n_inat_total"], ascending=[False, False, False]
    )
    cols = [
        "county_fips", "state", "county", "category", "cdc_status",
        "peak_window_obs", "n_inat_total", "n_neon_total",
        "last_detected_window", "rep_lat",
    ]
    cols = [c for c in cols if c in le.columns]
    return le[cols].head(n).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Output column order for the canonical table
# --------------------------------------------------------------------------- #
VALIDATION_COLUMNS = [
    "county_fips", "state", "county",
    "detected", "detected_cumulative", "detected_recent",
    "cdc_status", "established",
    "category", "category_label",
    "in_cdc_table",
    "peak_window_obs", "n_windows_detected", "n_windows_present",
    "n_inat_total", "n_neon_total", "ever_neon",
    "first_detected_window", "last_detected_window",
    "rep_lat", "rep_lon",
]


# --------------------------------------------------------------------------- #
# Top-level runner
# --------------------------------------------------------------------------- #
def run_stage5(write: bool = True) -> dict[str, Any]:
    """Build every Stage 5 artifact, optionally writing processed/reports/viz."""
    detections, cdc = load_inputs()

    summary = build_detection_summary(detections)
    validation = classify(summary, cdc)

    counties = _load_county_polygons()
    validation = attach_centroids(validation, counties)

    metrics = compute_metrics(validation)

    # Canonical table: stable column order; fill any missing (string) cols.
    cols = [c for c in VALIDATION_COLUMNS if c in validation.columns]
    table = validation[cols].sort_values("county_fips").reset_index(drop=True)

    fig_path = None
    web_path = None
    if write:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        table.to_parquet(
            config.PROCESSED_DIR / config.STAGE5_VALIDATION_PARQUET, index=False
        )
        table.to_csv(
            config.PROCESSED_DIR / config.STAGE5_VALIDATION_CSV,
            index=False, encoding="utf-8",
        )
        (config.PROCESSED_DIR / config.STAGE5_METRICS_JSON).write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )
        log.info("Wrote validation table + metrics -> %s", config.PROCESSED_DIR)

        fig_path = fig_validation_choropleth(validation, counties, metrics)
        web_path = export_web_layer(validation, counties)

    return {
        "validation": table,
        "metrics": metrics,
        "highlights": leading_edge_highlights(validation),
        "figure": fig_path,
        "web_layer": web_path,
    }


def main() -> int:
    out = run_stage5(write=True)
    m = out["metrics"]
    cm = m["confusion_matrix"]
    log.info("Stage 5 complete.")
    log.info(
        "Agreement | recall=%.1f%% precision=%.1f%% (lenient %.1f%%)",
        m["recall_of_cdc_established"] * 100, m["precision_strict"] * 100,
        m["precision_lenient_incl_reported"] * 100,
    )
    log.info(
        "Buckets | confirmed=%d  leading-edge=%d (reported %d / no-records %d)  "
        "blind-spot=%d",
        cm["TP_confirmed"], cm["FP_leading_edge"],
        cm["FP_leading_edge_reported"], cm["FP_leading_edge_no_records"],
        cm["FN_blind_spot"],
    )
    log.info("Most interesting leading-edge counties:")
    for r in out["highlights"].itertuples(index=False):
        log.info(
            "  %s %s, %s | %s | peak obs=%d (iNat=%d, NEON=%d) | last seen %s",
            r.county_fips, r.county, r.state, r.cdc_status, r.peak_window_obs,
            r.n_inat_total, r.n_neon_total, r.last_detected_window,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
