"""Stage 6 -- data-freshness guard for the monthly auto-refresh.

A GBIF outage (or a throttled, partial pull) must NEVER overwrite a working
public map with an empty or degraded one. This guard runs in CI AFTER the
pipeline (acquire -> clean -> stage3 -> stage4 web export) and BEFORE the Pages
deploy. It compares the freshly produced processed layers to the last known-good
run and runs a handful of cheap sanity checks. On any failure it exits non-zero
so the workflow stops loudly and the previous deployment stays live.

Checks
------
1. Non-empty: the cleaned target (A. americanum) count is > 0, and there is at
   least one cell-window and one rolling window.
2. No collapse: the target count has not dropped by more than a configurable
   fraction (default 20%, env ``REFRESH_MAX_DROP``) versus the last-good run.
3. Internal sanity of the effort-corrected cells: numerator <= denominator, and
   both ``raw_ratio`` and ``shrunk_ratio`` lie in [0, 1].

On success (and unless ``--no-update``), it rewrites the last-good counts file
so the next run compares against this one. ``meta.json`` is rewritten by the
Stage 4 export itself, so it is not touched here.

Usage
-----
    python src/refresh_guard.py                 # check + update on success
    python src/refresh_guard.py --no-update     # check only (dry run)
    REFRESH_MAX_DROP=0.30 python src/refresh_guard.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

import pandas as pd

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,
)
log = logging.getLogger("refresh_guard")

# Last-good counts persisted across runs (committed, so CI has a baseline).
LAST_GOOD_FILE = config.PROCESSED_DIR / "last_good_counts.json"
# Default maximum tolerated drop in the target count vs. the last-good run.
DEFAULT_MAX_DROP = 0.20


class GuardError(RuntimeError):
    """A freshness/sanity check failed; the workflow must not deploy."""


def _row_count(filename: str) -> int:
    path = config.PROCESSED_DIR / filename
    if not path.exists():
        raise GuardError(f"Expected processed output missing: {path}")
    return int(len(pd.read_parquet(path, columns=["gbifID"])))


def _max_drop() -> float:
    raw = os.getenv("REFRESH_MAX_DROP")
    if not raw:
        return DEFAULT_MAX_DROP
    try:
        val = float(raw)
    except ValueError as exc:
        raise GuardError(f"REFRESH_MAX_DROP is not a number: {raw!r}") from exc
    if not 0.0 < val < 1.0:
        raise GuardError(f"REFRESH_MAX_DROP must be in (0, 1); got {val}.")
    return val


def load_last_good() -> dict[str, Any] | None:
    if not LAST_GOOD_FILE.exists():
        log.info("No last-good baseline yet (%s); first run is exempt from the "
                 "drop check.", LAST_GOOD_FILE.name)
        return None
    try:
        return json.loads(LAST_GOOD_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise GuardError(f"Could not read {LAST_GOOD_FILE}: {exc}") from exc


def check_cells_sanity() -> int:
    """Validate the effort-corrected cells; return the cell-window row count."""
    path = config.PROCESSED_DIR / config.STAGE3_EFFORT_CELLS_FILE
    if not path.exists():
        raise GuardError(f"Stage 3 cells missing: {path}")
    cells = pd.read_parquet(
        path, columns=["numerator", "denominator", "raw_ratio", "shrunk_ratio"]
    )
    if cells.empty:
        raise GuardError("Effort-corrected cells table is empty.")
    if not (cells["numerator"] <= cells["denominator"]).all():
        bad = int((cells["numerator"] > cells["denominator"]).sum())
        raise GuardError(f"{bad} cell-windows have numerator > denominator.")
    for col in ("raw_ratio", "shrunk_ratio"):
        if not cells[col].between(0.0, 1.0).all():
            raise GuardError(f"{col} has values outside [0, 1].")
    log.info("Cells sanity OK: %d cell-windows, proportions in [0,1], "
             "numerator <= denominator.", len(cells))
    return int(len(cells))


def run_guard(update: bool) -> dict[str, Any]:
    max_drop = _max_drop()

    target = _row_count(config.PROCESSED_TARGET_FILE)
    background = _row_count(config.PROCESSED_BACKGROUND_FILE)
    if target <= 0:
        raise GuardError("Cleaned target (A. americanum) count is 0 -- refusing "
                         "to deploy an empty map.")

    n_cells = check_cells_sanity()

    frontier_path = config.PROCESSED_DIR / config.STAGE3_FRONTIER_METRICS_FILE
    if not frontier_path.exists():
        raise GuardError(f"Frontier metrics missing: {frontier_path}")
    n_windows = int(len(pd.read_csv(frontier_path)))
    if n_windows <= 0:
        raise GuardError("No rolling windows in the frontier table.")

    prev = load_last_good()
    if prev and prev.get("target", 0) > 0:
        prev_target = int(prev["target"])
        drop = (prev_target - target) / prev_target
        log.info("Target count: last-good=%d -> new=%d (change %+.1f%%).",
                 prev_target, target, -drop * 100)
        if drop > max_drop:
            raise GuardError(
                f"Target count dropped {drop:.1%} (last-good {prev_target} -> "
                f"{target}), exceeding the {max_drop:.0%} threshold. Likely a "
                "GBIF outage or partial pull -- NOT deploying."
            )
    else:
        log.info("Target count: new=%d (no baseline to compare).", target)

    record = {
        "target": target,
        "background": background,
        "cells": n_cells,
        "windows": n_windows,
        "data_vintage": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"),
        "updated_utc": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "max_drop_threshold": max_drop,
    }

    if update:
        LAST_GOOD_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
        log.info("Updated last-good baseline -> %s", LAST_GOOD_FILE.name)
    else:
        log.info("--no-update: not writing %s", LAST_GOOD_FILE.name)

    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 6 data-freshness guard.")
    ap.add_argument("--no-update", action="store_true",
                    help="run checks only; do not rewrite the last-good file.")
    args = ap.parse_args(argv)
    try:
        rec = run_guard(update=not args.no_update)
    except GuardError as exc:
        log.error("FRESHNESS GUARD FAILED: %s", exc)
        return 1
    log.info("Freshness guard PASSED: %s", json.dumps(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
