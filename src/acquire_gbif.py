"""Stage 1 -- GBIF occurrence acquisition.

Pulls georeferenced US occurrence records from GBIF for:
  1. the lone star tick (Amblyomma americanum)        -> the analysis backbone
  2. all hard ticks (family Ixodidae)                  -> observation-effort denom

Design notes
------------
* The taxonKey is resolved at runtime from the GBIF backbone (never hardcoded).
* Two pull methods are supported:
    - "download"  : the GBIF *download* API. Complete, reproducible, citable
                    (returns a DOI), but requires a free GBIF account whose
                    credentials are read from the environment (.env).
    - "search"    : the public occurrence/search API. No credentials, but GBIF
                    caps deep paging at 100k records, so it is only a fallback /
                    quick-look. Manifests record which method produced a file.
* Raw pulls land in data/raw/ unmodified, stamped with pull date + record count,
  beside a <file>.manifest.json provenance sidecar. Existing raw files are never
  overwritten.
* Coordinates are kept in EPSG:4326. No reprojection here.

Run as a script:
    python src/acquire_gbif.py            # both taxa, auto method
    python src/acquire_gbif.py --method search   # force public API fallback
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import functools
import json
import logging
import queue
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pygbif import occurrences as gbif_occ
from pygbif import species as gbif_species

import config

GBIF_API = "https://api.gbif.org/v1"


@functools.lru_cache(maxsize=1)
def _session() -> requests.Session:
    """Shared, connection-pooled session with urllib3 retries.

    urllib3's Retry honours the ``Retry-After`` header on 429s, so sustained
    paging against the public search endpoint backs off politely instead of
    hammering (which silently stalls pygbif's session-less requests.get).
    """
    session = requests.Session()
    # Few, fast retries: once GBIF starts throttling anonymous deep paging it
    # accepts the socket but never responds, so a long read timeout * many
    # retries would hang past any window. We want a blocked page to FAIL fast so
    # the caller can stop and persist a (partial) sample.
    retry = Retry(
        total=config.SEARCH_RETRY_TOTAL,
        backoff_factor=config.HTTP_BACKOFF_FACTOR,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=config.SEARCH_WORKERS,
        pool_maxsize=config.SEARCH_WORKERS,
    )
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,         # override any root handler an imported lib already set
)
log = logging.getLogger("acquire_gbif")

# GBIF search API caps deep paging here.
SEARCH_API_MAX_RECORDS = 100_000
SEARCH_PAGE_SIZE = 300


# --------------------------------------------------------------------------- #
# Retry helper (exponential backoff) -- API etiquette
# --------------------------------------------------------------------------- #
def with_retries(fn: Callable) -> Callable:
    """Retry transient failures with exponential backoff per config settings."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - we re-raise after retries
                last_exc = exc
                if attempt == config.HTTP_MAX_RETRIES:
                    break
                sleep_s = config.HTTP_BACKOFF_FACTOR ** attempt
                log.warning(
                    "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                    fn.__name__, attempt, config.HTTP_MAX_RETRIES, exc, sleep_s,
                )
                time.sleep(sleep_s)
        raise RuntimeError(f"{fn.__name__} failed after retries") from last_exc

    return wrapper


# --------------------------------------------------------------------------- #
# Taxonomy resolution
# --------------------------------------------------------------------------- #
@with_retries
def resolve_taxon_key(name: str, rank: str) -> dict:
    """Resolve a name to a GBIF backbone taxonKey. Logs what was matched.

    Returns a normalized dict: {usageKey, scientificName, rank, matchType,
    confidence, raw}. Handles both the legacy flat response and the newer
    nested ``{usage: {...}, diagnostics: {...}}`` backbone-match response.
    """
    # pygbif >=0.6.6 uses scientificName / taxonRank (not name / rank).
    raw = gbif_species.name_backbone(
        scientificName=name, taxonRank=rank, strict=True
    )
    usage = raw.get("usage", raw)            # nested (new) or flat (legacy)
    diagnostics = raw.get("diagnostics", raw)
    usage_key = usage.get("key", raw.get("usageKey"))
    match_type = diagnostics.get("matchType", raw.get("matchType"))

    if not usage_key or match_type == "NONE":
        raise ValueError(f"GBIF backbone could not resolve {name!r} ({rank}).")

    resolved = {
        "usageKey": int(usage_key),
        "scientificName": usage.get("name", raw.get("scientificName")),
        "rank": usage.get("rank", rank),
        "matchType": match_type,
        "confidence": diagnostics.get("confidence", raw.get("confidence")),
        "raw": raw,
    }
    log.info(
        "Resolved %r (%s) -> taxonKey=%s | scientificName=%r | "
        "matchType=%s confidence=%s",
        name, rank, resolved["usageKey"], resolved["scientificName"],
        resolved["matchType"], resolved["confidence"],
    )
    return resolved


# --------------------------------------------------------------------------- #
# Counts
# --------------------------------------------------------------------------- #
def _search_params(taxon_key: int, limit: int, offset: int) -> dict:
    return {
        "taxonKey": taxon_key,
        "country": config.COUNTRY_CODE,
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
        "limit": limit,
        "offset": offset,
    }


@with_retries
def occurrence_count(taxon_key: int) -> int:
    """Number of records matching the Stage 1 filters for a taxonKey."""
    resp = _session().get(
        f"{GBIF_API}/occurrence/search",
        params=_search_params(taxon_key, limit=0, offset=0),
        timeout=config.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return int(resp.json()["count"])


# --------------------------------------------------------------------------- #
# Filename / manifest helpers
# --------------------------------------------------------------------------- #
def _today() -> str:
    return dt.date.today().isoformat()


def _stamped_path(slug: str, count: int, ext: str) -> Path:
    """data/raw/<slug>_us_<date>_n<count>.<ext> -- never overwritten."""
    fname = f"{slug}_us_{_today()}_n{count}.{ext}"
    path = config.RAW_DIR / fname
    if path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing raw file: {path}. "
            "Raw pulls are immutable; delete by hand if you really mean to."
        )
    return path


def write_manifest(data_path: Path, manifest: dict) -> Path:
    manifest_path = data_path.with_suffix(data_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Wrote provenance manifest: %s", manifest_path.name)
    return manifest_path


# --------------------------------------------------------------------------- #
# Method 1: GBIF download API (preferred, needs credentials)
# --------------------------------------------------------------------------- #
def _have_credentials() -> bool:
    return all((config.GBIF_USER, config.GBIF_PWD, config.GBIF_EMAIL))


def _download_predicate(taxon_key: int) -> list[str]:
    """Predicate query strings for pygbif's download() helper."""
    return [
        f"taxonKey = {taxon_key}",
        f"country = {config.COUNTRY_CODE}",
        "hasCoordinate = true",
        "hasGeospatialIssue = false",
    ]


@with_retries
def _request_download(taxon_key: int) -> str:
    queries = _download_predicate(taxon_key)
    log.info("Requesting GBIF download with predicate: %s", queries)
    res = gbif_occ.download(
        queries,
        format="SIMPLE_CSV",
        user=config.GBIF_USER,
        pwd=config.GBIF_PWD,
        email=config.GBIF_EMAIL,
    )
    # pygbif returns [key, meta] (older) or just key depending on version.
    key = res[0] if isinstance(res, (list, tuple)) else res
    log.info("GBIF accepted download request. download key = %s", key)
    return key


def _poll_download(key: str, poll_every_s: int = 20, max_wait_s: int = 3600) -> dict:
    """Block until a GBIF download is SUCCEEDED (or fail loudly)."""
    waited = 0
    while True:
        meta = gbif_occ.download_meta(key)
        status = meta.get("status")
        log.info("Download %s status=%s (waited %ds)", key, status, waited)
        if status == "SUCCEEDED":
            return meta
        if status in {"KILLED", "CANCELLED", "FAILED"}:
            raise RuntimeError(f"GBIF download {key} ended with status {status}.")
        if waited >= max_wait_s:
            raise TimeoutError(
                f"GBIF download {key} not ready after {max_wait_s}s "
                f"(last status={status}). Re-run later; key is reusable."
            )
        time.sleep(poll_every_s)
        waited += poll_every_s


def pull_via_download_api(
    slug: str, taxon_key: int, taxon_label: str, existing_key: str | None = None
) -> Path:
    """Full reproducible pull via the GBIF download API. Returns raw zip path.

    Pass ``existing_key`` to resume/fetch a download that was already requested
    (GBIF prepares downloads asynchronously and keeps them retrievable by key),
    instead of submitting a new request.
    """
    key = existing_key or _request_download(taxon_key)
    if existing_key:
        log.info("Resuming existing GBIF download key = %s", key)
    meta = _poll_download(key)
    doi = meta.get("doi")
    count = int(meta.get("totalRecords", 0))
    log.info("Download %s ready: %d records, DOI=%s", key, count, doi)

    zip_path = _stamped_path(slug, count, "zip")
    # download_get pulls to a directory; grab the bytes ourselves for an
    # exact, stamped, immutable raw artifact.
    tmp_dir = config.CACHE_DIR / f"gbif_{key}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    gbif_occ.download_get(key, path=str(tmp_dir))
    src_zip = tmp_dir / f"{key}.zip"
    zip_path.write_bytes(src_zip.read_bytes())
    log.info("Saved raw GBIF download zip -> %s", zip_path.name)

    # Extract the occurrence table to interim/ for downstream consumption
    # (raw zip stays pristine in data/raw).
    with zipfile.ZipFile(zip_path) as zf:
        member = next(m for m in zf.namelist() if m.endswith(".csv"))
        out_csv = config.INTERIM_DIR / f"{slug}_us_{_today()}_n{count}.csv"
        with zf.open(member) as fsrc, open(out_csv, "wb") as fdst:
            fdst.write(fsrc.read())
    log.info("Extracted occurrence table -> interim/%s", out_csv.name)

    write_manifest(
        zip_path,
        {
            "taxon_label": taxon_label,
            "taxon_key": taxon_key,
            "country": config.COUNTRY_CODE,
            "filters": config.GBIF_BASE_FILTERS,
            "fields_min_required": config.GBIF_FIELDS,
            "method": "download_api",
            "format": "SIMPLE_CSV",
            "gbif_download_key": key,
            "gbif_doi": doi,
            "gbif_citation": meta.get("license"),
            "pull_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "record_count": count,
            "raw_file": zip_path.name,
            "interim_extracted_csv": out_csv.name,
            "crs": config.RAW_CRS,
            "note": (
                "Cite via the GBIF DOI above. iNaturalist research-grade records "
                "are included via their GBIF dataset."
            ),
        },
    )
    return zip_path


# --------------------------------------------------------------------------- #
# Method 2: public occurrence/search API (fallback, capped at 100k)
# --------------------------------------------------------------------------- #
def _search_page(taxon_key: int, offset: int) -> dict:
    # Hit the GBIF API directly via a pooled session (keep-alive + Retry-After
    # aware) rather than pygbif's session-less requests.get, which stalls under
    # sustained concurrent paging. The session already retries (urllib3), so no
    # extra @with_retries wrapper here -- on persistent failure we want it to
    # raise promptly so the caller can stop gracefully and write a partial pull.
    resp = _session().get(
        f"{GBIF_API}/occurrence/search",
        params=_search_params(taxon_key, limit=SEARCH_PAGE_SIZE, offset=offset),
        timeout=config.SEARCH_TIMEOUT,  # (connect, read) -- short read so we fail fast
    )
    resp.raise_for_status()
    return resp.json()


def _search_page_bounded(taxon_key: int, offset: int) -> dict:
    """_search_page with a HARD wall-clock bound.

    When GBIF throttles anonymous paging it can trickle bytes so requests' read
    timeout (inactivity-based) never fires and a page hangs for minutes. We run
    the fetch in a daemon thread and abandon it after SEARCH_PAGE_HARD_TIMEOUT,
    raising TimeoutError. Daemon => a hung socket can never block process exit.
    """
    result_q: queue.Queue = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_q.put(("ok", _search_page(taxon_key, offset)))
        except Exception as exc:  # noqa: BLE001 - surfaced to caller below
            result_q.put(("err", exc))

    threading.Thread(target=_worker, daemon=True).start()
    try:
        kind, value = result_q.get(timeout=config.SEARCH_PAGE_HARD_TIMEOUT)
    except queue.Empty as exc:
        raise TimeoutError(
            f"page at offset {offset} exceeded "
            f"{config.SEARCH_PAGE_HARD_TIMEOUT}s hard timeout"
        ) from exc
    if kind == "err":
        raise value
    return value


def pull_via_search_api(slug: str, taxon_key: int, taxon_label: str) -> Path:
    """Fallback pull via the public search API. Returns raw CSV path.

    NOTE: capped at GBIF's 100k deep-paging limit. If the true count exceeds the
    cap, the file is a (chronologically arbitrary) subset -- the manifest records
    this so it is never mistaken for the complete set.
    """
    total = occurrence_count(taxon_key)
    target = min(total, SEARCH_API_MAX_RECORDS)
    truncated = total > SEARCH_API_MAX_RECORDS
    if truncated:
        log.warning(
            "%s has %d records (> %d search-API cap). Pulling first %d only. "
            "Use the download API for the complete set.",
            taxon_label, total, SEARCH_API_MAX_RECORDS, target,
        )

    # Sequential paging over a single keep-alive connection. This is the most
    # robust approach for the public endpoint: GBIF intermittently drops
    # connections under sustained automated load, and concurrency just multiplies
    # the broken connections / pool contention. urllib3 Retry (Retry-After aware)
    # recovers per page; a small inter-page delay keeps us polite.
    offsets = list(range(0, target, SEARCH_PAGE_SIZE))
    rows: list[dict] = []
    partial = False
    stop_reason: str | None = None
    for i, off in enumerate(offsets, start=1):
        try:
            page = _search_page_bounded(taxon_key, off)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            # GBIF rate-limits sustained anonymous paging from one IP (it drops
            # connections after a few thousand records). Rather than spin until
            # killed, stop and persist what we have, clearly flagged as partial.
            partial = True
            stop_reason = f"page at offset {off} failed: {type(exc).__name__}: {exc}"
            log.warning("Stopping search pull early -- %s", stop_reason)
            log.warning(
                "This is the GBIF anonymous-paging throttle; use the download "
                "API (credentials) for the complete, citable set."
            )
            break
        for r in page.get("results", []):
            rows.append({k: r.get(k) for k in config.GBIF_FIELDS})
        if i % 20 == 0 or i == len(offsets):
            log.info("  %s: fetched %d/%d pages (%d rows)",
                     taxon_label, i, len(offsets), len(rows))
        if page.get("endOfRecords"):
            break
        if config.SEARCH_PAGE_DELAY_SECONDS:
            time.sleep(config.SEARCH_PAGE_DELAY_SECONDS)

    count = len(rows)
    csv_path = _stamped_path(slug, count, "csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=config.GBIF_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Saved raw search-API pull -> %s (%d records)", csv_path.name, count)

    write_manifest(
        csv_path,
        {
            "taxon_label": taxon_label,
            "taxon_key": taxon_key,
            "country": config.COUNTRY_CODE,
            "filters": config.GBIF_BASE_FILTERS,
            "fields_kept": config.GBIF_FIELDS,
            "method": "search_api",
            "gbif_total_matching_count": total,
            "record_count": count,
            "truncated_at_cap": truncated,
            "search_api_cap": SEARCH_API_MAX_RECORDS,
            "partial_pull": partial,
            "stopped_early_reason": stop_reason,
            "completeness": (
                round(count / total, 4) if total else None
            ),
            "pull_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "raw_file": csv_path.name,
            "crs": config.RAW_CRS,
            "note": (
                "Fallback method (no GBIF credentials). NOT citable via DOI and "
                "incomplete -- GBIF throttles anonymous deep paging after a few "
                "thousand records. Re-pull via the download API for the complete, "
                "citable set."
            ),
        },
    )
    return csv_path


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def acquire_taxon(
    slug: str, name: str, rank: str, method: str, download_key: str | None = None
) -> dict:
    match = resolve_taxon_key(name, rank)
    taxon_key = int(match["usageKey"])
    count = occurrence_count(taxon_key)
    log.info("%s (taxonKey=%d): %d matching US georeferenced records.",
             name, taxon_key, count)

    use_download = (
        method == "download" or download_key is not None
        or (method == "auto" and _have_credentials())
    )
    if method == "download" and not download_key and not _have_credentials():
        raise RuntimeError(
            "method=download requested but GBIF credentials are missing. "
            "Set GBIF_USER / GBIF_PWD / GBIF_EMAIL in .env."
        )

    if use_download:
        path = pull_via_download_api(slug, taxon_key, name, existing_key=download_key)
    else:
        log.warning(
            "No GBIF credentials found -- falling back to public search API for "
            "%s. (Set credentials in .env for the full, citable download.)", name,
        )
        path = pull_via_search_api(slug, taxon_key, name)

    return {"taxon": name, "taxon_key": taxon_key, "count": count,
            "raw_path": str(path), "method": "download" if use_download else "search"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GBIF Stage 1 acquisition.")
    parser.add_argument(
        "--method", choices=["auto", "download", "search"], default="auto",
        help="auto: use download API if credentials present, else search API.",
    )
    parser.add_argument(
        "--only", choices=["primary", "background", "both"], default="both",
        help="Which taxa to pull.",
    )
    parser.add_argument(
        "--download-key", default=None,
        help="Resume/fetch an already-requested GBIF download by key "
             "(requires --only primary or background, not both).",
    )
    args = parser.parse_args(argv)

    if args.download_key and args.only == "both":
        parser.error("--download-key requires --only primary OR background.")

    log.info("GBIF credentials present: %s", _have_credentials())
    results = []
    if args.only in {"primary", "both"}:
        results.append(acquire_taxon(
            "gbif_amblyomma_americanum",
            config.PRIMARY_SPECIES_NAME, config.PRIMARY_RANK, args.method,
            download_key=args.download_key,
        ))
    if args.only in {"background", "both"}:
        results.append(acquire_taxon(
            "gbif_ixodidae_background",
            config.BACKGROUND_FAMILY_NAME, config.BACKGROUND_RANK, args.method,
            download_key=args.download_key,
        ))

    log.info("Done. Summary:")
    for r in results:
        log.info("  %s", json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
