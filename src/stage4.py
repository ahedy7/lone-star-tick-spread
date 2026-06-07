"""Stage 4 -- presentation-grade visualization.

Turns the Stage 3 processed layers into the project's two visual deliverables:

* an **interactive, animated frontier map** (the centerpiece) under ``viz/`` --
  a self-contained deck.gl ``H3HexagonLayer`` app that opens straight from the
  filesystem, sweeps the rolling windows, and (the key interaction) toggles
  between the *raw* and *effort-corrected* surfaces so a viewer can SEE the
  observer-effort bias being removed; and
* a set of **polished static figures** under ``reports/figures/`` for the
  README -- raw-vs-corrected hex maps, the northern-limit advance, and the
  centroid convergence.

This module only **reads** ``data/processed/`` (the Stage 3 outputs) and only
**writes** to ``viz/`` and ``reports/figures/``. Nothing here touches
``data/raw/`` or ``data/interim/``. All tunables live in ``config.py``.

Honest-framing guardrails baked into every caption produced here:

* the headline is the **raw-vs-corrected contrast** and the **northern-limit
  advance** (~70 km over the decade);
* the **centroid contrast** shows observer-effort bias being removed (raw
  count-weighting is pulled toward high-volume southern metros; share-weighting
  is not), converging as coverage fills in;
* **occupied-cell growth is partly a coverage artifact** and is never presented
  as pure range expansion.

Run as a script to regenerate every Stage 4 artifact:
    python src/stage4.py                 # web export + static figures + GIF
    python src/stage4.py --no-gif        # skip the (slower) animation export
    python src/stage4.py --only web      # web export only
"""

from __future__ import annotations

import argparse
import datetime as dt
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
log = logging.getLogger("stage4")


# --------------------------------------------------------------------------- #
# Load (processed only)
# --------------------------------------------------------------------------- #
def load_stage3() -> dict[str, pd.DataFrame]:
    """Load the Stage 3 layers Stage 4 needs, from data/processed/ only."""
    p = config.PROCESSED_DIR
    paths = {
        "cells": p / config.STAGE3_EFFORT_CELLS_FILE,
        "neon": p / config.STAGE3_NEON_PRESENCE_FILE,
        "frontier": p / config.STAGE3_FRONTIER_METRICS_FILE,
    }
    for name, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(
                f"Stage 3 output missing: {path}. Run `python src/stage3.py` first."
            )
    cells = pd.read_parquet(paths["cells"])
    neon = pd.read_parquet(paths["neon"])
    frontier = pd.read_csv(paths["frontier"])
    log.info(
        "Loaded Stage 3: cells=%d rows, neon=%d rows, frontier=%d windows",
        len(cells), len(neon), len(frontier),
    )
    return {"cells": cells, "neon": neon, "frontier": frontier}


def ordered_windows(cells: pd.DataFrame) -> list[str]:
    """Rolling windows in chronological order (by window_start)."""
    return (
        cells[["window", "window_start"]]
        .drop_duplicates()
        .sort_values("window_start")["window"]
        .tolist()
    )


def latest_window(cells: pd.DataFrame) -> str:
    return ordered_windows(cells)[-1]


# --------------------------------------------------------------------------- #
# Shared cartography: colormap stops (so JS == matplotlib exactly)
# --------------------------------------------------------------------------- #
def colormap_stops(n: int | None = None) -> list[list[int]]:
    """Sample the configured matplotlib colormap to ``n`` RGB stops (0-255).

    Handed to the JS renderer so the interactive ramp is identical to the
    static figures.
    """
    import matplotlib as mpl

    n = n or config.VIZ_COLOR_STOPS
    cmap = mpl.colormaps[config.VIZ_COLORMAP].resampled(n)
    return [[int(round(c * 255)) for c in cmap(i)[:3]] for i in range(n)]


# --------------------------------------------------------------------------- #
# Data-vintage stamp (Stage 6): a small, machine-readable provenance block the
# deployed map shows and the monthly auto-refresh rewrites each run.
# --------------------------------------------------------------------------- #
def _target_record_count() -> int | None:
    """Count of cleaned A. americanum occurrences, or None if unreadable."""
    path = config.PROCESSED_DIR / config.PROCESSED_TARGET_FILE
    if not path.exists():
        return None
    try:
        return int(len(pd.read_parquet(path, columns=["gbifID"])))
    except Exception as exc:  # pragma: no cover - provenance is best-effort
        log.warning("Could not count target records for meta (%s).", exc)
        return None


def build_vintage(windows: list[str]) -> dict[str, Any]:
    """Build the data-vintage stamp embedded in the bundle and written to meta.json.

    ``dataVintage`` is the build month (YYYY-MM): the citizen-science frontier is
    a monthly-refreshed pull, so the build month is its honest vintage. The CDC
    layer carries its own ANNUAL vintage separately so the UI can state, plainly,
    that the validation layer is a periodic checkpoint and not live.
    """
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "dataVintage": now.strftime("%Y-%m"),
        "lastUpdated": now.date().isoformat(),
        "generatedUtc": now.replace(microsecond=0).isoformat(),
        "latestWindow": windows[-1] if windows else None,
        "windowCount": len(windows),
        "targetRecords": _target_record_count(),
        "frontierSource": "GBIF / iNaturalist (citizen science), refreshed monthly",
        "cdcVintage": config.CDC_DATA_VINTAGE,
        "cdcNote": (
            "CDC establishment is an annual vintage and updates rarely and by "
            "hand; it is a periodic spatial checkpoint, not a live layer."
        ),
    }


def write_site_meta(vintage: dict[str, Any]) -> Path:
    """Write viz/data/meta.json (the stamp the deployed map reads / the workflow updates)."""
    config.VIZ_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.VIZ_DATA_DIR / config.VIZ_META_JSON
    out.write_text(json.dumps(vintage, indent=2), "utf-8")
    log.info(
        "Wrote data-vintage stamp -> %s (vintage=%s, %s records)",
        out.name, vintage["dataVintage"], vintage.get("targetRecords"),
    )
    return out


# --------------------------------------------------------------------------- #
# Task 1 -- web data export (compact JSON keyed by window) + JS bundle
# --------------------------------------------------------------------------- #
def export_web_data(layers: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Export the deck.gl payload: a compact JSON keyed by window + a JS bundle.

    cells.json -> {meta, windows:{<window>:[{h,r,s}, ...]}} where h=h3_cell,
    r=raw_ratio, s=shrunk_ratio (rounded). The H3HexagonLayer renders hexagons
    straight from the H3 ids, so no polygon geometry is exported.
    """
    cells = layers["cells"]
    neon = layers["neon"]
    frontier = layers["frontier"]
    dec = config.VIZ_RATIO_DECIMALS
    windows = ordered_windows(cells)
    default_window = config.VIZ_DEFAULT_WINDOW or windows[-1]

    # --- cells, keyed by window (compact keys, rounded values) -------------- #
    cells_by_window: dict[str, list[dict[str, Any]]] = {}
    for win in windows:
        g = cells[cells["window"] == win]
        recs = [
            {"h": h, "r": round(float(r), dec), "s": round(float(s), dec)}
            for h, r, s in zip(g["h3_cell"], g["raw_ratio"], g["shrunk_ratio"])
        ]
        cells_by_window[win] = recs

    # Frontier band + baseline + per-window edge lines for the interactive
    # overlay and the "frontier band" zoom preset.
    frontier_lines = compute_frontier_lines(cells, frontier)
    nl_by_window = dict(
        zip(frontier["window"], frontier["corrected_northern_limit_lat"])
    )
    baseline_lat = float(nl_by_window[windows[0]])

    meta = {
        "windows": windows,
        "defaultWindow": default_window,
        "defaultSurface": config.VIZ_DEFAULT_SURFACE,
        "colorStops": colormap_stops(),
        "colorDomain": list(config.VIZ_COLOR_DOMAIN),
        "colormapName": config.VIZ_COLORMAP,
        "rateLabel": config.VIZ_RATE_LABEL,
        "hexOpacity": config.VIZ_HEX_OPACITY,
        "animMsPerWindow": config.VIZ_ANIM_MS_PER_WINDOW,
        "basemapStyleUrl": config.VIZ_BASEMAP_STYLE_URL,
        "initialViewState": {
            "latitude": config.VIZ_INITIAL_LATITUDE,
            "longitude": config.VIZ_INITIAL_LONGITUDE,
            "zoom": config.VIZ_INITIAL_ZOOM,
        },
        "h3Resolution": config.H3_RESOLUTION,
        "frontierBand": {
            "latMin": config.VIZ_FRONTIER_LAT_MIN,
            "latMax": config.VIZ_FRONTIER_LAT_MAX,
            "lonMin": config.VIZ_FRONTIER_LON_MIN,
            "lonMax": config.VIZ_FRONTIER_LON_MAX,
        },
        "frontierMethod": config.VIZ_FRONTIER_METHOD,
        "frontierBaselineLat": baseline_lat,
        "kmPerDegLat": config.VIZ_KM_PER_DEG_LAT,
        "northernLimitByWindow": {
            w: round(float(v), 4) for w, v in nl_by_window.items()
        },
    }
    # Embed the data-vintage stamp in the bundle meta so the deployed map always
    # has it without a second fetch (works even from file://); write meta.json
    # alongside as the canonical, workflow-rewritten copy.
    vintage = build_vintage(windows)
    meta["vintage"] = vintage
    write_site_meta(vintage)
    cells_payload = {"meta": meta, "windows": cells_by_window}

    # --- NEON presence overlay, keyed by window ----------------------------- #
    neon_by_window: dict[str, list[dict[str, Any]]] = {}
    for win in windows:
        g = neon[neon["window"] == win]
        neon_by_window[win] = [
            {
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "detected": bool(d),
            }
            for lat, lon, d in zip(
                g["cell_lat"], g["cell_lon"], g["lone_star_detected"]
            )
        ]

    # --- frontier metrics for the on-map info panel ------------------------- #
    frontier_records = [
        {
            "window": r.window,
            "rawNorthernLimit": round(float(r.raw_northern_limit_lat), 3),
            "corNorthernLimit": round(float(r.corrected_northern_limit_lat), 3),
            "rawCentroid": round(float(r.raw_centroid_lat), 3),
            "corCentroid": round(float(r.corrected_centroid_lat), 3),
            "rawOccupied": int(r.raw_occupied_cells),
            "corOccupied": int(r.corrected_occupied_cells),
        }
        for r in frontier.itertuples(index=False)
    ]

    # --- write the canonical JSON deliverables ------------------------------ #
    config.VIZ_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_cells = config.VIZ_DATA_DIR / config.VIZ_CELLS_JSON
    out_neon = config.VIZ_DATA_DIR / config.VIZ_NEON_JSON
    out_frontier = config.VIZ_DATA_DIR / config.VIZ_FRONTIER_JSON
    out_cells.write_text(json.dumps(cells_payload, separators=(",", ":")), "utf-8")
    out_neon.write_text(
        json.dumps(neon_by_window, separators=(",", ":")), "utf-8"
    )
    out_frontier.write_text(
        json.dumps(frontier_records, separators=(",", ":")), "utf-8"
    )

    # --- frontier edge lines (per window) ----------------------------------- #
    out_lines = config.VIZ_DATA_DIR / config.VIZ_FRONTIER_LINES_JSON
    out_lines.write_text(
        json.dumps(frontier_lines, separators=(",", ":")), "utf-8"
    )

    # --- JS bundle so the page opens from file:// with no server ------------ #
    bundle = {
        "cells": cells_payload,
        "neon": neon_by_window,
        "frontier": frontier_records,
        "frontierLines": frontier_lines,
    }
    out_bundle = config.VIZ_DATA_DIR / config.VIZ_BUNDLE_JS
    out_bundle.write_text(
        "window.LST_DATA = " + json.dumps(bundle, separators=(",", ":")) + ";",
        "utf-8",
    )

    n_records = sum(len(v) for v in cells_by_window.values())
    size_kb = out_bundle.stat().st_size / 1024
    log.info(
        "Web export: %d cell-window records across %d windows -> %s (%.0f KB), "
        "%s, %s; JS bundle -> %s",
        n_records, len(windows), out_cells.name, out_cells.stat().st_size / 1024,
        out_neon.name, out_frontier.name, f"{out_bundle.name} ({size_kb:.0f} KB)",
    )
    return {"meta": meta, "n_records": n_records}


# --------------------------------------------------------------------------- #
# Static figures -- shared helpers
# --------------------------------------------------------------------------- #
def _apply_style() -> None:
    """Consistent, intentional typography for every static figure."""
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.dpi": 110,
        "savefig.dpi": config.VIZ_FIG_DPI,
        "savefig.bbox": "tight",
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.titlesize": 14,
        "axes.titleweight": "bold",
        "axes.labelsize": 11,
        "axes.edgecolor": "#3a3a3a",
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "grid.color": "#d9d9d9",
        "grid.linewidth": 0.6,
        "legend.frameon": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


# Brand palette for the raw-vs-corrected line story (color-blind safe).
RAW_COLOR = "#c44e52"        # warm red -- the biased, uncorrected surface
CORR_COLOR = "#2f6f9f"       # cool blue -- the effort-corrected surface
INK = "#1a1a1a"
MUTED = "#666666"


def _hex_geodataframe(g: pd.DataFrame, value_col: str) -> "Any":
    """Build a Web-Mercator GeoDataFrame of H3 cell polygons for one window."""
    import geopandas as gpd
    import h3
    from shapely.geometry import Polygon

    polys, vals = [], []
    for h, v in zip(g["h3_cell"], g[value_col]):
        boundary = h3.cell_to_boundary(h)  # [(lat, lon), ...]
        polys.append(Polygon([(lon, lat) for lat, lon in boundary]))
        vals.append(float(v))
    gdf = gpd.GeoDataFrame({value_col: vals}, geometry=polys, crs=config.RAW_CRS)
    return gdf.to_crs(epsg=3857)


def _frame_bounds_mercator(
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Project a lon/lat frame to Web Mercator (x0,y0,x1,y1).

    ``bounds`` is (lon_min, lat_min, lon_max, lat_max); default is the
    configured CONUS-east frame.
    """
    import geopandas as gpd
    from shapely.geometry import box

    if bounds is None:
        bounds = (config.VIZ_LON_MIN, config.VIZ_LAT_MIN,
                  config.VIZ_LON_MAX, config.VIZ_LAT_MAX)
    frame = gpd.GeoSeries([box(*bounds)], crs=config.RAW_CRS).to_crs(epsg=3857)
    minx, miny, maxx, maxy = frame.total_bounds
    return minx, miny, maxx, maxy


def _to_mercator(lons, lats):
    """Vectorised lon/lat (EPSG:4326) -> Web Mercator (EPSG:3857) x, y arrays."""
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, 3857, always_xy=True)
    return tf.transform(np.asarray(lons), np.asarray(lats))


def _smooth_nan(arr: np.ndarray, window: int) -> np.ndarray:
    """Centered moving average that skips NaNs; window<=1 returns the input."""
    if window <= 1:
        return arr
    half = window // 2
    out = np.full_like(arr, np.nan, dtype=float)
    n = len(arr)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        seg = arr[lo:hi]
        seg = seg[np.isfinite(seg)]
        if seg.size:
            out[i] = seg.mean()
    return out


def compute_frontier_lines(
    cells: pd.DataFrame, frontier: pd.DataFrame
) -> dict[str, list[list[float]]]:
    """Per-window corrected northern-edge line as a list of [lon, lat] points.

    ``per_longitude`` -> the (lightly smoothed) northernmost positive-cell
    latitude per longitude bin across the frontier band; ``percentile`` -> a
    flat line at the corrected 95th-percentile northern limit from the frontier
    table. Positive = shrunk_ratio >= the Stage 3 occupied threshold.
    """
    thr = config.OCCUPIED_INTENSITY_THRESHOLD
    lon_min, lon_max = config.VIZ_FRONTIER_LON_MIN, config.VIZ_FRONTIER_LON_MAX
    wins = ordered_windows(cells)

    if config.VIZ_FRONTIER_METHOD == "percentile":
        nl = dict(zip(frontier["window"], frontier["corrected_northern_limit_lat"]))
        return {
            w: [[lon_min, float(nl[w])], [lon_max, float(nl[w])]]
            for w in wins if w in nl and np.isfinite(nl[w])
        }

    step = config.VIZ_FRONTIER_LON_STEP
    hw = config.VIZ_FRONTIER_LON_HALFWIDTH
    pct = config.VIZ_FRONTIER_PERCENTILE * 100.0
    min_cells = config.VIZ_FRONTIER_MIN_BIN_CELLS
    centers = np.arange(lon_min, lon_max + step / 2.0, step)
    lines: dict[str, list[list[float]]] = {}
    for win in wins:
        g = cells[(cells["window"] == win) & (cells["shrunk_ratio"] >= thr)]
        lon = g["cell_lon"].to_numpy()
        lat = g["cell_lat"].to_numpy()
        lats = np.full(len(centers), np.nan)
        for k, c in enumerate(centers):
            pool = lat[np.abs(lon - c) <= hw]
            if pool.size >= min_cells:
                lats[k] = float(np.percentile(pool, pct))
        lats = _smooth_nan(lats, config.VIZ_FRONTIER_SMOOTH_BINS)
        lines[win] = [
            [float(c), float(l)] for c, l in zip(centers, lats) if np.isfinite(l)
        ]
    return lines


def _add_basemap(ax, zoom: int = 5) -> bool:
    """Add the CARTO Positron tile basemap. Returns False if tiles unreachable.

    The caller MUST set the axis x/y limits to the target frame first -- the
    tile region and zoom are inferred from the current axis extent. On failure
    the caller falls back to a styled land/state base so figures still render
    offline.
    """
    try:
        import contextily as cx

        provider = cx.providers.CartoDB.Positron
        cx.add_basemap(ax, source=provider, zoom=zoom, attribution_size=6)
        return True
    except Exception as exc:  # network / tile failure -> graceful fallback
        log.warning("Basemap tiles unavailable (%s); using styled state base.", exc)
        return False


def _add_state_base(ax) -> None:
    """Offline fallback base: land fill + state outlines from cached counties."""
    try:
        import geopandas as gpd

        cache = config.CACHE_DIR / config.COUNTY_GEOJSON_CACHE
        counties = gpd.read_file(cache)
        if counties.crs is None:
            counties = counties.set_crs(config.RAW_CRS)
        counties = counties.to_crs(epsg=3857)
        states = counties.dissolve().to_crs(epsg=3857)
        states.plot(ax=ax, color="#eef1f4", edgecolor="none", zorder=0)
        counties.boundary.plot(ax=ax, color="#cfd6dd", linewidth=0.2, zorder=1)
    except Exception as exc:  # pragma: no cover - last-ditch base
        log.warning("State-base fallback failed (%s); plotting on blank axes.", exc)
        ax.set_facecolor("#eef1f4")


def _norm():
    import matplotlib.colors as mcolors

    lo, hi = config.VIZ_COLOR_DOMAIN
    return mcolors.Normalize(vmin=lo, vmax=hi, clip=True)


# --------------------------------------------------------------------------- #
# Figure 1 -- raw vs corrected hex map, most recent window, shared legend
# --------------------------------------------------------------------------- #
def fig_raw_vs_corrected(cells: pd.DataFrame) -> Path:
    import matplotlib.pyplot as plt

    _apply_style()
    win = config.VIZ_DEFAULT_WINDOW or latest_window(cells)
    g = cells[cells["window"] == win].copy()
    # Frame to CONUS-east so a couple of far-flung coarse cells don't zoom out.
    g = g[
        (g["cell_lon"].between(config.VIZ_LON_MIN, config.VIZ_LON_MAX))
        & (g["cell_lat"].between(config.VIZ_LAT_MIN, config.VIZ_LAT_MAX))
    ]
    norm = _norm()
    cmap = config.VIZ_COLORMAP
    minx, miny, maxx, maxy = _frame_bounds_mercator()
    panel_aspect = (maxx - minx) / (maxy - miny)

    fig, axes = plt.subplots(1, 2, figsize=(7.3 * panel_aspect * 2 + 0.4, 8.0))
    panels = [
        (axes[0], "raw_ratio", "RAW  ·  noisy single-sighting cells"),
        (axes[1], "shrunk_ratio", "CORRECTED  ·  effort share + shrinkage"),
    ]
    for ax, col, subtitle in panels:
        # Limits BEFORE the basemap so contextily fetches the right tiles/zoom.
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_aspect("equal")
        if not _add_basemap(ax):
            _add_state_base(ax)
        gdf = _hex_geodataframe(g, col)
        gdf.plot(ax=ax, column=col, cmap=cmap, norm=norm,
                 alpha=0.9, edgecolor="none", zorder=3)
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(subtitle, fontsize=12.5, color=INK, pad=8)
        ax.grid(False)
        for s in ax.spines.values():
            s.set_edgecolor("#cccccc")

    fig.subplots_adjust(left=0.012, right=0.988, top=0.9, bottom=0.16, wspace=0.03)

    cax = fig.add_axes([0.30, 0.105, 0.40, 0.022])
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cbar.set_label(config.VIZ_RATE_LABEL, fontsize=10.5, color=INK)
    cbar.ax.tick_params(labelsize=9, color="#888888")

    fig.suptitle(
        f"Lone star tick: raw vs. effort-corrected share  ({win})",
        fontsize=16, fontweight="bold", color=INK, y=0.965,
    )
    fig.text(
        0.5, 0.055,
        "Same window, same cells, same colour scale. The raw share is pinned to "
        "0 or 1 in thin, single-observation cells (scattered dark specks); "
        "empirical-Bayes shrinkage pulls those toward the regional rate while "
        "well-sampled cells barely move — leaving the stable, effort-corrected "
        "signal on the right.",
        ha="center", va="top", fontsize=9.5, color=MUTED, wrap=True,
    )
    out = config.FIGURES_DIR / config.VIZ_FIG_RAW_VS_CORR
    fig.savefig(out)
    plt.close(fig)
    log.info("Wrote figure -> %s", out)
    return out


# --------------------------------------------------------------------------- #
# Figure 2 -- northern-limit latitude, raw vs corrected, ~70 km advance
# --------------------------------------------------------------------------- #
def fig_northern_limit(frontier: pd.DataFrame) -> Path:
    import matplotlib.pyplot as plt

    _apply_style()
    f = frontier.sort_values("window_start").reset_index(drop=True)
    x = np.arange(len(f))
    raw = f["raw_northern_limit_lat"].to_numpy()
    cor = f["corrected_northern_limit_lat"].to_numpy()
    km = config.VIZ_KM_PER_DEG_LAT
    raw_adv = (raw[-1] - raw[0]) * km
    cor_adv = (cor[-1] - cor[0]) * km

    fig, ax = plt.subplots(figsize=(11, 6.4))
    ax.plot(x, raw, "-o", color=RAW_COLOR, lw=2.4, ms=6,
            label="raw count surface", zorder=3)
    ax.plot(x, cor, "-o", color=CORR_COLOR, lw=2.4, ms=6,
            label="effort-corrected surface", zorder=3)

    # Net-advance bracket on the corrected line (the honest headline).
    ax.annotate(
        "", xy=(x[-1], cor[-1]), xytext=(x[-1], cor[0]),
        arrowprops=dict(arrowstyle="<->", color=INK, lw=1.3),
    )
    ax.hlines([cor[0], cor[-1]], x[-1] - 0.35, x[-1] + 0.05,
              color=INK, lw=0.8, linestyles=(0, (3, 3)))
    ax.annotate(
        f"northern limit advances\n~{cor_adv:.0f} km (corrected),"
        f" ~{raw_adv:.0f} km (raw)\nover the decade",
        xy=(x[-1] - 0.12, (cor[0] + cor[-1]) / 2),
        xytext=(0.7, cor.max() - 0.06),
        fontsize=10, color=INK, va="top",
        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8),
        bbox=dict(boxstyle="round,pad=0.4", fc="#fbf7ef", ec="#e2d9c5"),
    )

    ax.set_xticks(x)
    ax.set_xticklabels(f["window"], rotation=35, ha="right", fontsize=9)
    ax.set_ylabel("northern range limit  (95th-pct latitude of positive cells, °N)")
    ax.set_title("Lone star tick: the northern edge is moving north")
    ax.legend(loc="lower right", fontsize=10)
    secax = ax.secondary_yaxis(
        "right",
        functions=(lambda d: (d - raw.min()) * km, lambda k: k / km + raw.min()),
    )
    secax.set_ylabel("km north of the 2015–17 raw limit", color=MUTED, fontsize=9)
    fig.text(
        0.5, -0.02,
        "Northern limit = the 95th-percentile latitude of positive cells "
        "(robust to a single northern outlier), per surface. Raw and corrected "
        "tell the same northward story; the advance is the real signal.",
        ha="center", va="top", fontsize=9.5, color=MUTED, wrap=True,
    )
    out = config.FIGURES_DIR / config.VIZ_FIG_NORTHERN_LIMIT
    fig.savefig(out)
    plt.close(fig)
    log.info("Wrote figure -> %s (raw advance %.0f km, corrected %.0f km)",
             out, raw_adv, cor_adv)
    return out


# --------------------------------------------------------------------------- #
# Figure 3 -- centroid latitude, raw vs corrected, convergence
# --------------------------------------------------------------------------- #
def fig_centroid(frontier: pd.DataFrame) -> Path:
    import matplotlib.pyplot as plt

    _apply_style()
    f = frontier.sort_values("window_start").reset_index(drop=True)
    x = np.arange(len(f))
    raw = f["raw_centroid_lat"].to_numpy()
    cor = f["corrected_centroid_lat"].to_numpy()
    km = config.VIZ_KM_PER_DEG_LAT
    gap0 = (cor[0] - raw[0]) * km
    gap1 = (cor[-1] - raw[-1]) * km

    fig, ax = plt.subplots(figsize=(11, 6.4))
    ax.fill_between(x, raw, cor, color="#cfd9e6", alpha=0.55, zorder=1,
                    label="effort-bias gap")
    ax.plot(x, raw, "-o", color=RAW_COLOR, lw=2.4, ms=6,
            label="raw centroid (count-weighted)", zorder=3)
    ax.plot(x, cor, "-o", color=CORR_COLOR, lw=2.4, ms=6,
            label="corrected centroid (share-weighted)", zorder=3)

    ax.annotate(
        f"~{gap0:.0f} km gap:\nraw counts pulled south\ntoward high-volume metros",
        xy=(x[0] + 0.05, (raw[0] + cor[0]) / 2),
        xytext=(x[0] + 0.9, raw[0] - 0.18),
        fontsize=9.5, color=INK, va="top",
        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8),
        bbox=dict(boxstyle="round,pad=0.4", fc="#f3f6fa", ec="#cfd9e6"),
    )
    ax.annotate(
        f"converged (~{abs(gap1):.0f} km):\neffort bias shrinks\nas coverage fills in",
        xy=(x[-1] - 0.1, (raw[-1] + cor[-1]) / 2),
        xytext=(x[-1] - 2.9, raw.min() + 0.18),
        fontsize=9.5, color=INK, va="bottom", ha="left",
        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8),
        bbox=dict(boxstyle="round,pad=0.4", fc="#f3f6fa", ec="#cfd9e6"),
    )

    ax.set_xticks(x)
    ax.set_xticklabels(f["window"], rotation=35, ha="right", fontsize=9)
    ax.set_ylabel("intensity-weighted centroid latitude  (°N)")
    ax.set_title("Lone star tick: watching observer-effort bias get removed")
    ax.legend(loc="upper right", fontsize=9.5)
    fig.text(
        0.5, -0.02,
        "Raw count-weighting drags the centroid south toward dense southern "
        "metros; share-weighting does not. The shrinking gap is effort bias "
        "being removed as northern coverage fills in — not, by itself, a range "
        "shift.",
        ha="center", va="top", fontsize=9.5, color=MUTED, wrap=True,
    )
    out = config.FIGURES_DIR / config.VIZ_FIG_CENTROID
    fig.savefig(out)
    plt.close(fig)
    log.info("Wrote figure -> %s (gap %.0f km -> %.0f km)", out, gap0, gap1)
    return out


# --------------------------------------------------------------------------- #
# Optional -- animated GIF of the corrected surface sweeping the windows
# --------------------------------------------------------------------------- #
def export_corrected_gif(cells: pd.DataFrame) -> Path | None:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter

    _apply_style()
    windows = ordered_windows(cells)
    norm = _norm()
    cmap = config.VIZ_COLORMAP
    minx, miny, maxx, maxy = _frame_bounds_mercator()

    clip = cells[
        (cells["cell_lon"].between(config.VIZ_LON_MIN, config.VIZ_LON_MAX))
        & (cells["cell_lat"].between(config.VIZ_LAT_MIN, config.VIZ_LAT_MAX))
    ]

    fig, ax = plt.subplots(figsize=(8.5, 8.6))
    fig.subplots_adjust(left=0.03, right=0.97, top=0.92, bottom=0.13)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    have_tiles = _add_basemap(ax)  # fetch tiles once; reuse across frames
    if not have_tiles:
        _add_state_base(ax)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cax = fig.add_axes([0.30, 0.085, 0.40, 0.016])
    cbar = fig.colorbar(sm, cax=cax, orientation="horizontal")
    cbar.set_label(config.VIZ_RATE_LABEL, fontsize=9)
    cbar.ax.tick_params(labelsize=8)
    fig.suptitle("Lone star tick — effort-corrected share by window",
                 fontsize=14, fontweight="bold", color=INK, y=0.965)
    fig.text(
        0.5, 0.035,
        "Corrected surface (share-weighted, EB-shrunk). Cell growth is partly a "
        "coverage artifact; the northward edge is the signal.",
        ha="center", va="top", fontsize=8.5, color=MUTED,
    )
    label = ax.text(
        0.03, 0.965, "", transform=ax.transAxes, ha="left", va="top",
        fontsize=15, fontweight="bold", color=INK,
        bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#cccccc", alpha=0.85),
    )
    drawn: list[Any] = []

    def update(win: str):
        for art in drawn:
            art.remove()
        drawn.clear()
        g = clip[clip["window"] == win]
        gdf = _hex_geodataframe(g, "shrunk_ratio")
        coll = gdf.plot(
            ax=ax, column="shrunk_ratio", cmap=cmap, norm=norm,
            alpha=0.82, edgecolor="white", linewidth=0.1, zorder=3,
        ).collections[-1]
        drawn.append(coll)
        label.set_text(win)
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        return drawn + [label]

    anim = FuncAnimation(fig, update, frames=windows, interval=700, blit=False)
    out = config.FIGURES_DIR / config.VIZ_GIF_CORRECTED
    try:
        anim.save(str(out), writer=PillowWriter(fps=config.VIZ_GIF_FPS),
                  dpi=130, savefig_kwargs={"facecolor": "white"})
    except Exception as exc:
        log.warning("GIF export failed (%s); skipping.", exc)
        plt.close(fig)
        return None
    plt.close(fig)
    log.info("Wrote animation -> %s (%d frames)", out, len(windows))
    return out


# --------------------------------------------------------------------------- #
# Hero animation -- frontier band, cropped, with an advancing northern edge
# --------------------------------------------------------------------------- #
def export_frontier_advance_gif(
    cells: pd.DataFrame, frontier: pd.DataFrame
) -> Path | None:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter

    _apply_style()
    windows = ordered_windows(cells)
    norm = _norm()
    cmap = config.VIZ_COLORMAP
    band = (config.VIZ_FRONTIER_LON_MIN, config.VIZ_FRONTIER_LAT_MIN,
            config.VIZ_FRONTIER_LON_MAX, config.VIZ_FRONTIER_LAT_MAX)
    minx, miny, maxx, maxy = _frame_bounds_mercator(band)

    # Cells inside the band (a small margin so edge hexes aren't clipped hard).
    m = 0.6
    clip = cells[
        cells["cell_lon"].between(config.VIZ_FRONTIER_LON_MIN - m,
                                  config.VIZ_FRONTIER_LON_MAX + m)
        & cells["cell_lat"].between(config.VIZ_FRONTIER_LAT_MIN - m,
                                    config.VIZ_FRONTIER_LAT_MAX + m)
    ]
    lines = compute_frontier_lines(cells, frontier)
    nl_by_window = dict(
        zip(frontier["window"], frontier["corrected_northern_limit_lat"])
    )
    baseline_lat = float(nl_by_window[windows[0]])
    km_per_deg = config.VIZ_KM_PER_DEG_LAT
    thr = config.OCCUPIED_INTENSITY_THRESHOLD

    from matplotlib.lines import Line2D

    panel_aspect = (maxx - minx) / (maxy - miny)
    fig, ax = plt.subplots(figsize=(12.5, 12.5 / panel_aspect + 1.6))
    fig.subplots_adjust(left=0.02, right=0.9, top=0.87, bottom=0.17)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    if not _add_basemap(ax, zoom=config.VIZ_FRONTIER_BASEMAP_ZOOM):
        _add_state_base(ax)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)

    # Static baseline edge (first window), so the climb is visible against it.
    base_pts = lines.get(windows[0], [])
    if base_pts:
        bx, by = _to_mercator([p[0] for p in base_pts], [p[1] for p in base_pts])
        ax.plot(bx, by, color="#5b6b7a", lw=1.5, ls=(0, (4, 3)), alpha=0.75,
                zorder=5)

    # Vertical colourbar on the right (the band map is wide + short).
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cax = fig.add_axes([0.915, 0.26, 0.013, 0.46])
    cbar = fig.colorbar(sm, cax=cax, orientation="vertical")
    cbar.set_label(config.VIZ_RATE_LABEL, fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    legend_handles = [
        Line2D([0], [0], color="#b3123f", lw=2.6, label="corrected northern edge"),
        Line2D([0], [0], color="#5b6b7a", lw=1.5, ls=(0, (4, 3)),
               label="2015–17 baseline edge"),
    ]
    if config.VIZ_FRONTIER_HIGHLIGHT_NEW:
        legend_handles.append(
            Line2D([0], [0], marker="o", color="none",
                   markeredgecolor=config.VIZ_FRONTIER_HIGHLIGHT_COLOR,
                   markerfacecolor="none", markersize=8,
                   label="newly occupied (this window)")
        )
    ax.legend(handles=legend_handles, loc="lower left", fontsize=8.5,
              framealpha=0.92, edgecolor="#cccccc").set_zorder(9)

    fig.suptitle("Lone star tick — the corrected northern edge, advancing",
                 fontsize=16, fontweight="bold", color=INK, y=0.965)
    fig.text(
        0.46, 0.105,
        "View cropped to the frontier band (Midwest → Northeast). Solid line = "
        "corrected northern edge (per-longitude 95th-pct latitude of positive "
        "cells).\nFill-in elsewhere is partly a coverage artifact — the ~66 km "
        "edge advance is the real signal, not the area growth.",
        ha="center", va="top", fontsize=9, color=MUTED, linespacing=1.5,
    )
    label = ax.text(
        0.02, 0.96, "", transform=ax.transAxes, ha="left", va="top",
        fontsize=17, fontweight="bold", color=INK,
        bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#cccccc", alpha=0.92),
    )
    readout = ax.text(
        0.02, 0.85, "", transform=ax.transAxes, ha="left", va="top",
        fontsize=12, color=INK,
        bbox=dict(boxstyle="round,pad=0.3", fc="#fbf7ef", ec="#e2d9c5", alpha=0.94),
    )

    drawn: list[Any] = []

    def update(i: int):
        win = windows[i]
        for art in drawn:
            art.remove()
        drawn.clear()

        g = clip[clip["window"] == win]
        gdf = _hex_geodataframe(g, "shrunk_ratio")
        coll = gdf.plot(
            ax=ax, column="shrunk_ratio", cmap=cmap, norm=norm,
            alpha=0.9, edgecolor="none", zorder=3,
        ).collections[-1]
        drawn.append(coll)

        # Newly occupied cells north of the prior window's edge.
        if config.VIZ_FRONTIER_HIGHLIGHT_NEW and i > 0:
            prior = lines.get(windows[i - 1], [])
            if prior:
                plon = np.array([p[0] for p in prior])
                plat = np.array([p[1] for p in prior])
                order = np.argsort(plon)
                pos = g[g["shrunk_ratio"] >= thr]
                if len(pos):
                    edge_at = np.interp(
                        pos["cell_lon"].to_numpy(), plon[order], plat[order]
                    )
                    new = pos[pos["cell_lat"].to_numpy() > edge_at]
                    if len(new):
                        nx, ny = _to_mercator(
                            new["cell_lon"].to_numpy(), new["cell_lat"].to_numpy()
                        )
                        sc = ax.scatter(
                            nx, ny, s=34, facecolors="none",
                            edgecolors=config.VIZ_FRONTIER_HIGHLIGHT_COLOR,
                            linewidths=1.1, zorder=6,
                        )
                        drawn.append(sc)

        # Current advancing edge.
        pts = lines.get(win, [])
        if pts:
            lx, ly = _to_mercator([p[0] for p in pts], [p[1] for p in pts])
            halo, = ax.plot(lx, ly, color="white", lw=5.0, alpha=0.85, zorder=7)
            line, = ax.plot(lx, ly, color="#b3123f", lw=2.6, zorder=8)
            drawn.extend([halo, line])

        nl = float(nl_by_window[win])
        adv = (nl - baseline_lat) * km_per_deg
        label.set_text(win)
        readout.set_text(
            f"corrected northern limit: {nl:.2f}°N\n"
            f"+{adv:.0f} km north of 2015–17"
        )
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        return drawn + [label, readout]

    anim = FuncAnimation(fig, update, frames=range(len(windows)),
                         interval=700, blit=False)
    out = config.FIGURES_DIR / config.VIZ_GIF_FRONTIER
    try:
        anim.save(str(out), writer=PillowWriter(fps=config.VIZ_GIF_FRONTIER_FPS),
                  dpi=130, savefig_kwargs={"facecolor": "white"})
    except Exception as exc:
        log.warning("Frontier GIF export failed (%s); skipping.", exc)
        plt.close(fig)
        return None
    plt.close(fig)
    log.info("Wrote hero animation -> %s (%d frames, method=%s)",
             out, len(windows), config.VIZ_FRONTIER_METHOD)
    return out


# --------------------------------------------------------------------------- #
# Top-level runner
# --------------------------------------------------------------------------- #
def run_stage4(do_web: bool = True, do_figs: bool = True,
               do_gif: bool = True) -> dict[str, Any]:
    layers = load_stage3()
    result: dict[str, Any] = {}
    if do_web:
        result["web"] = export_web_data(layers)
    if do_figs:
        result["fig_raw_vs_corrected"] = fig_raw_vs_corrected(layers["cells"])
        result["fig_northern_limit"] = fig_northern_limit(layers["frontier"])
        result["fig_centroid"] = fig_centroid(layers["frontier"])
    if do_gif:
        # The frontier-band advance is the primary (hero) animation.
        result["gif_frontier"] = export_frontier_advance_gif(
            layers["cells"], layers["frontier"]
        )
        if config.VIZ_KEEP_CONUS_GIF:
            result["gif_conus"] = export_corrected_gif(layers["cells"])
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4 -- visualization.")
    ap.add_argument(
        "--only", choices=["web", "figs", "gif"], default=None,
        help="run only one stage of the build (default: all).",
    )
    ap.add_argument("--no-gif", action="store_true",
                    help="skip the (slower) animation export.")
    args = ap.parse_args()

    if args.only == "web":
        run_stage4(do_web=True, do_figs=False, do_gif=False)
    elif args.only == "figs":
        run_stage4(do_web=False, do_figs=True, do_gif=False)
    elif args.only == "gif":
        run_stage4(do_web=False, do_figs=False, do_gif=True)
    else:
        run_stage4(do_web=True, do_figs=True, do_gif=not args.no_gif)
    log.info("Stage 4 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
