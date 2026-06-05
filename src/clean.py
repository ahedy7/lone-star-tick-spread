"""Stage 2 -- cleaning + harmonization.

Turns the raw GBIF SIMPLE_CSV extracts in data/interim/ into analysis-ready
tables in data/processed/, and tidies the CDC establishment workbook. NOTHING
here touches data/raw/ or data/interim/ -- processed outputs are regenerable.

Design principles
-----------------
* IDENTICAL cleaning for target (A. americanum) and background (Ixodidae). The
  background is the observation-effort denominator, so it must pass through the
  exact same functions as the numerator. ``clean_occurrences`` does both.
* Every transformation is logged with before/after row counts into a cleaning
  ledger (a list of step records), so the notebook can render a full audit.
* Sources are kept SEPARABLE via a ``source_type`` column (NEON / iNaturalist /
  other), classified primarily by GBIF publishingOrganizationKey. See config for
  why blank-institutionCode records are NEON drag-cloth sampling, not iNat.
* Dedup deliberately preserves NEON within-site replication (see config).

All tunables (thresholds, H3 resolution, CRS, dedup keys, dense-window start)
live in config.py.

Run as a script to (re)generate every processed output:
    python src/clean.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from pyproj import Transformer

import config

try:
    import h3
except ImportError as exc:  # pragma: no cover - dependency is pinned
    raise ImportError(
        "h3 is required for Stage 2 spatial binning. `pip install h3` "
        "(pinned in requirements.txt)."
    ) from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,
)
log = logging.getLogger("clean")

# Raw columns we need to read from the SIMPLE_CSV (superset of what we keep; the
# extras -- issue, day -- are consumed during cleaning then dropped).
_READ_COLUMNS = [
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
    "eventDate",
    "year",
    "month",
    "issue",
]

# One reusable WGS84 -> CONUS Albers transformer (always_xy => lon/lat in).
_TO_ALBERS = Transformer.from_crs(
    config.RAW_CRS, config.ANALYSIS_CRS, always_xy=True
)


# --------------------------------------------------------------------------- #
# Ledger helper
# --------------------------------------------------------------------------- #
def _log_step(
    ledger: list[dict[str, Any]],
    dataset: str,
    step: str,
    rows_in: int,
    rows_out: int,
    note: str = "",
) -> None:
    """Append a before/after row-count record and echo it to the log."""
    removed = rows_in - rows_out
    pct = round(100 * removed / rows_in, 3) if rows_in else 0.0
    ledger.append(
        {
            "dataset": dataset,
            "step": step,
            "rows_in": rows_in,
            "rows_out": rows_out,
            "removed": removed,
            "pct_removed": pct,
            "note": note,
        }
    )
    log.info(
        "[%s] %-28s %8d -> %8d (removed %6d, %5.2f%%) %s",
        dataset, step, rows_in, rows_out, removed, pct, note,
    )


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def load_interim(filename: str) -> pd.DataFrame:
    """Read a GBIF SIMPLE_CSV extract from data/interim/ (tab-separated)."""
    path = config.INTERIM_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Interim file not found: {path}. Did Stage 1 run? "
            "Check config.STAGE2_INPUTS filenames."
        )
    df = pd.read_csv(
        path, sep=config.INTERIM_SEP, usecols=_READ_COLUMNS, low_memory=False
    )
    log.info("Loaded %s (%d rows, %d cols)", filename, len(df), df.shape[1])
    return df


# --------------------------------------------------------------------------- #
# Column + type standardization (no row removal)
# --------------------------------------------------------------------------- #
def standardize_types(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce coordinates to float and derive clean integer year/month.

    Prefers GBIF's interpreted ``year``/``month`` (already parsed from
    eventDate); falls back to parsing the eventDate string where they are null.
    """
    df = df.copy()
    df["decimalLatitude"] = pd.to_numeric(df["decimalLatitude"], errors="coerce")
    df["decimalLongitude"] = pd.to_numeric(df["decimalLongitude"], errors="coerce")
    df["coordinateUncertaintyInMeters"] = pd.to_numeric(
        df["coordinateUncertaintyInMeters"], errors="coerce"
    )

    year = pd.to_numeric(df["year"], errors="coerce")
    month = pd.to_numeric(df["month"], errors="coerce")
    # Fallback: pull YYYY / MM off the ISO-ish eventDate string where missing.
    ev = df["eventDate"].astype("string")
    year = year.fillna(pd.to_numeric(ev.str.slice(0, 4), errors="coerce"))
    month = month.fillna(pd.to_numeric(ev.str.slice(5, 7), errors="coerce"))
    df["year"] = year.astype("Int64")
    df["month"] = month.astype("Int64")
    return df


# --------------------------------------------------------------------------- #
# Source tagging (the critical step) -- no row removal
# --------------------------------------------------------------------------- #
def tag_source(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``source_type`` in {NEON, iNaturalist, other}, kept separable.

    Priority: publishingOrgKey (most robust) -> datasetKey -> institutionCode.
    """
    df = df.copy()
    org = df["publishingOrgKey"]
    dskey = df["datasetKey"]
    inst = df["institutionCode"].astype("string").str.strip()

    is_neon = (
        org.eq(config.NEON_PUBLISHING_ORG_KEY)
        | dskey.isin(config.NEON_DATASET_KEYS)
        | inst.str.casefold().eq("neon")
    )
    is_inat = (
        org.eq(config.INATURALIST_PUBLISHING_ORG_KEY)
        | dskey.eq(config.INATURALIST_RG_DATASET_KEY)
        | inst.str.casefold().eq("inaturalist")
    )

    source = pd.Series(config.SRC_OTHER, index=df.index, dtype="object")
    # NEON first so the rare record matching both signals lands on systematic.
    source[is_inat] = config.SRC_INAT
    source[is_neon] = config.SRC_NEON
    df["source_type"] = source
    return df


# --------------------------------------------------------------------------- #
# Coordinate quality (row removal, each rule logged)
# --------------------------------------------------------------------------- #
def _has_issue(issue_series: pd.Series, flags: frozenset[str]) -> pd.Series:
    """Boolean mask: does the ';'-delimited GBIF issue string contain any flag?"""
    tokens = issue_series.fillna("").str.split(";")
    flagset = set(flags)
    return tokens.apply(lambda toks: any(t in flagset for t in toks))


def apply_coordinate_quality(
    df: pd.DataFrame, dataset: str, ledger: list[dict[str, Any]]
) -> pd.DataFrame:
    """Drop null/zero coords, broken-coordinate issue flags, and over-uncertain
    records. Adds a ``coordinate_rounded`` flag (kept, not dropped)."""
    # Flag (don't drop) rounded coordinates.
    df = df.copy()
    df["coordinate_rounded"] = _has_issue(df["issue"], config.FLAG_COORDINATE_ISSUES)

    # 1. Null coordinates.
    n = len(df)
    df = df[df["decimalLatitude"].notna() & df["decimalLongitude"].notna()]
    _log_step(ledger, dataset, "drop null coordinates", n, len(df))

    # 2. Exact (0, 0) coordinates ("null island").
    n = len(df)
    df = df[~((df["decimalLatitude"] == 0) & (df["decimalLongitude"] == 0))]
    _log_step(ledger, dataset, "drop zero coordinates", n, len(df))

    # 3. Known broken-coordinate GBIF issue flags.
    n = len(df)
    bad = _has_issue(df["issue"], config.DROP_COORDINATE_ISSUES)
    df = df[~bad]
    _log_step(
        ledger, dataset, "drop bad coord issue flags", n, len(df),
        note=f"flags={sorted(config.DROP_COORDINATE_ISSUES)}",
    )

    # 4. Coordinate uncertainty -> hex-reliability FLAG, not a drop. Records
    #    coarser than the threshold can't be placed confidently in a res-5 hex
    #    (~17 km across) so they are excluded from the fine density surface, but
    #    they stay usable at county level (CDC validation / frontier in Stage 3),
    #    so every record is KEPT. NaN/absent uncertainty -> reliable (as before).
    over = (
        df["coordinateUncertaintyInMeters"] > config.MAX_COORDINATE_UNCERTAINTY_M
    ).fillna(False)
    df["hex_reliable"] = ~over
    by_src = df.loc[over, "source_type"].value_counts().to_dict()
    _log_step(
        ledger, dataset, "flag hex_reliable=False", len(df), len(df),
        note=(f"max={config.MAX_COORDINATE_UNCERTAINTY_M:.0f}m; flagged="
              f"{int(over.sum())} (kept, not dropped); by source_type={by_src}; "
              "NaN stays reliable"),
    )
    return df


# --------------------------------------------------------------------------- #
# Deduplication (row removal, logged)
# --------------------------------------------------------------------------- #
def deduplicate(
    df: pd.DataFrame, dataset: str, ledger: list[dict[str, Any]]
) -> pd.DataFrame:
    """Remove exact occurrenceID duplicates and cross-source coord/date
    collisions, preserving legitimate within-source (NEON) replication."""
    df = df.copy()

    # 1. Exact duplicate occurrenceID (the same record re-published). Rows with a
    #    null occurrenceID can't be keyed this way and are left untouched here.
    n = len(df)
    has_id = df["occurrenceID"].notna()
    dup_id = has_id & df["occurrenceID"].duplicated(keep="first")
    df = df[~dup_id]
    _log_step(ledger, dataset, "drop duplicate occurrenceID", n, len(df))

    # 2. Cross-source collisions: same species + rounded coords + date reaching
    #    GBIF via >1 source_type. Keep one; drop the rest. Within-source
    #    coincidences (NEON drag replication) are intentionally preserved.
    n = len(df)
    d = config.DEDUP_COORD_DECIMALS
    key = (
        df["species"].astype("string").fillna("")
        + "|" + df["decimalLatitude"].round(d).astype("string")
        + "|" + df["decimalLongitude"].round(d).astype("string")
        + "|" + df["eventDate"].astype("string").str.slice(0, 10).fillna("")
    )
    if config.DEDUP_CROSS_SOURCE_ONLY:
        nsrc = df.groupby(key)["source_type"].transform("nunique")
        in_multi = nsrc > 1
        drop = in_multi & key.duplicated(keep="first")
        note = "cross-source coord/date collisions only"
    else:
        drop = key.duplicated(keep="first")
        note = "all coord/date collisions"
    df = df[~drop]
    _log_step(ledger, dataset, "drop cross-source dups", n, len(df), note=note)
    return df


# --------------------------------------------------------------------------- #
# Reprojection + spatial binning + temporal (no row removal)
# --------------------------------------------------------------------------- #
def add_projection(df: pd.DataFrame) -> pd.DataFrame:
    """Add EPSG:5070 (CONUS Albers) x/y, keeping raw EPSG:4326 lon/lat."""
    df = df.copy()
    x, y = _TO_ALBERS.transform(
        df["decimalLongitude"].to_numpy(), df["decimalLatitude"].to_numpy()
    )
    df["x_5070"] = x
    df["y_5070"] = y
    return df


def add_h3(df: pd.DataFrame, resolution: int) -> pd.DataFrame:
    """Assign each record an H3 cell ID at the given resolution."""
    df = df.copy()
    lat = df["decimalLatitude"].to_numpy()
    lon = df["decimalLongitude"].to_numpy()
    df["h3_cell"] = [
        h3.latlng_to_cell(float(la), float(lo), resolution)
        for la, lo in zip(lat, lon)
    ]
    return df


def add_temporal(df: pd.DataFrame) -> pd.DataFrame:
    """Add the dense-analysis-window boolean (year >= configured start)."""
    df = df.copy()
    df["in_dense_window"] = df["year"] >= config.DENSE_SIGNAL_START_YEAR
    df["in_dense_window"] = df["in_dense_window"].fillna(False).astype(bool)
    return df


# --------------------------------------------------------------------------- #
# Orchestrator -- identical pipeline for target and background
# --------------------------------------------------------------------------- #
def clean_occurrences(
    dataset: str, filename: str, ledger: list[dict[str, Any]]
) -> pd.DataFrame:
    """Run the full Stage 2 occurrence pipeline for one dataset."""
    df = load_interim(filename)
    _log_step(ledger, dataset, "load interim", len(df), len(df), note=filename)

    df = standardize_types(df)
    _log_step(ledger, dataset, "standardize types", len(df), len(df))

    df = tag_source(df)
    _log_step(ledger, dataset, "tag source_type", len(df), len(df))

    df = apply_coordinate_quality(df, dataset, ledger)
    df = deduplicate(df, dataset, ledger)

    df = add_projection(df)
    df = add_h3(df, config.H3_RESOLUTION)
    df = add_temporal(df)
    _log_step(ledger, dataset, "reproject+h3+temporal", len(df), len(df),
              note=f"EPSG:5070, h3 res {config.H3_RESOLUTION}")

    keep = [c for c in config.STAGE2_KEEP_COLUMNS if c in df.columns]
    df = df[keep].reset_index(drop=True)
    _log_step(ledger, dataset, "trim to keep columns", len(df), len(df),
              note=f"{len(keep)} cols")
    return df


# --------------------------------------------------------------------------- #
# CDC harmonization (light)
# --------------------------------------------------------------------------- #
def harmonize_cdc() -> pd.DataFrame:
    """Tidy the CDC establishment workbook: 5-digit FIPS + clean status flags."""
    path = config.RAW_DIR / config.CDC_RAW_XLSX
    raw = pd.read_excel(path, sheet_name=config.CDC_SHEET)
    log.info("Loaded CDC workbook %s (%d rows)", config.CDC_RAW_XLSX, len(raw))

    status_col = next(c for c in raw.columns if "Status" in str(c))
    out = pd.DataFrame(
        {
            "county_fips": (
                pd.to_numeric(raw["FIPS"], errors="coerce")
                .astype("Int64")
                .astype("string")
                .str.zfill(5)
            ),
            "state": raw["State"].astype("string").str.strip(),
            "county": raw["County"].astype("string").str.strip(),
            "status": raw[status_col].astype("string").str.strip(),
            "source": raw.get("Source", pd.Series(index=raw.index, dtype="string")),
        }
    )
    out["established"] = out["status"].str.casefold().eq(
        config.CDC_ESTABLISHED_STATUS_VALUE.casefold()
    )
    bad_fips = out["county_fips"].isna() | (out["county_fips"] == "<NA>")
    if bad_fips.any():
        log.warning("CDC: %d rows with unparseable FIPS dropped", int(bad_fips.sum()))
        out = out[~bad_fips]
    log.info(
        "CDC tidy: %d counties | established=%d reported=%d other=%d",
        len(out),
        int(out["established"].sum()),
        int(out["status"].str.casefold().eq("reported").sum()),
        int((~out["established"] & ~out["status"].str.casefold().eq("reported")).sum()),
    )
    return out


# --------------------------------------------------------------------------- #
# Top-level runner -- writes all processed outputs
# --------------------------------------------------------------------------- #
def run_stage2(write: bool = True) -> dict[str, Any]:
    """Clean both occurrence datasets + CDC, optionally writing to processed/."""
    ledger: list[dict[str, Any]] = []
    target = clean_occurrences("target", config.STAGE2_INPUTS["target"], ledger)
    background = clean_occurrences(
        "background", config.STAGE2_INPUTS["background"], ledger
    )
    cdc = harmonize_cdc()

    if write:
        config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        tpath = config.PROCESSED_DIR / config.PROCESSED_TARGET_FILE
        bpath = config.PROCESSED_DIR / config.PROCESSED_BACKGROUND_FILE
        cpath = config.PROCESSED_DIR / config.PROCESSED_CDC_FILE
        lpath = config.PROCESSED_DIR / config.PROCESSED_LEDGER_FILE
        target.to_parquet(tpath, index=False)
        background.to_parquet(bpath, index=False)
        cdc.to_csv(cpath, index=False, encoding="utf-8")
        lpath.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        log.info("Wrote processed outputs -> %s", config.PROCESSED_DIR)

    return {
        "target": target,
        "background": background,
        "cdc": cdc,
        "ledger": pd.DataFrame(ledger),
    }


def main() -> int:
    out = run_stage2(write=True)
    log.info("Stage 2 complete.")
    for name in ("target", "background"):
        df = out[name]
        log.info("  %s: %d rows | source_type=%s",
                 name, len(df), dict(df["source_type"].value_counts()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
