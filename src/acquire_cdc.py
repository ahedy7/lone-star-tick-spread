"""Stage 1 -- CDC validation layer acquisition.

Downloads the CDC county-level *establishment* dataset for the lone star tick
(Amblyomma americanum), established counties through 2024, and saves the raw
file to data/raw/ untouched (stamped, never overwritten) with a provenance
manifest.

Important domain fact (also in the README): CDC "established" status is sticky /
monotonic. Once a county is recorded as established it stays established, so this
table is a *cumulative footprint*, not an annual snapshot.

The CDC surveillance site reorganizes periodically. This script tries, in order:
  1. an explicit --url you pass,
  2. links auto-discovered on the CDC landing page (config.CDC_LANDING_PAGE)
     that look like an Amblyomma americanum establishment CSV.
If neither resolves, it fails loudly with the landing page so a human can grab
the correct link and re-run with --url.

Run:
    python src/acquire_cdc.py
    python src/acquire_cdc.py --url https://www.cdc.gov/.../established_aa.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,  # stdout, not stderr (PowerShell treats stderr as errors)
    force=True,         # override any root handler an imported lib already set
)
log = logging.getLogger("acquire_cdc")


def build_session() -> requests.Session:
    """requests session with retry/backoff + a polite User-Agent."""
    session = requests.Session()
    retry = Retry(
        total=config.HTTP_MAX_RETRIES,
        backoff_factor=config.HTTP_BACKOFF_FACTOR,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session


def discover_csv_url(session: requests.Session) -> str | None:
    """Scan the CDC landing page for a lone-star-tick establishment CSV link."""
    log.info("Discovering dataset link from %s", config.CDC_LANDING_PAGE)
    resp = session.get(config.CDC_LANDING_PAGE, timeout=config.HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    html = resp.text

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    # Prefer links that mention the species/common name AND look like data files.
    species_pat = re.compile(r"(amblyomma|americanum|lone[-_ ]?star)", re.IGNORECASE)
    data_pat = re.compile(r"\.(csv|xlsx?|zip)(\?|$)", re.IGNORECASE)

    candidates = [
        urljoin(config.CDC_LANDING_PAGE, h)
        for h in hrefs
        if data_pat.search(h) and species_pat.search(h)
    ]
    if candidates:
        log.info("Found %d candidate link(s); using: %s", len(candidates), candidates[0])
        return candidates[0]

    # Fall back to any establishment-looking data file on the page.
    loose = [
        urljoin(config.CDC_LANDING_PAGE, h)
        for h in hrefs
        if data_pat.search(h) and re.search(r"establish", h, re.IGNORECASE)
    ]
    if loose:
        log.warning("No species-named link; using establishment data link: %s", loose[0])
        return loose[0]

    log.error("Could not auto-discover a dataset link on the CDC page.")
    return None


def _stamped_path(url: str, record_count: int | None) -> Path:
    ext = Path(url.split("?")[0]).suffix.lstrip(".") or "csv"
    n = f"_n{record_count}" if record_count is not None else ""
    fname = f"cdc_amblyomma_americanum_established_{dt.date.today().isoformat()}{n}.{ext}"
    path = config.RAW_DIR / fname
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite immutable raw file: {path}")
    return path


def download(url: str, session: requests.Session) -> Path:
    log.info("Downloading CDC dataset: %s", url)
    resp = session.get(url, timeout=config.HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    content = resp.content

    # Best-effort record count (CSV line count minus header) for the stamp.
    record_count: int | None = None
    if url.split("?")[0].lower().endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
            record_count = max(text.count("\n") - 1, 0)
        except UnicodeDecodeError:
            record_count = None

    path = _stamped_path(url, record_count)
    path.write_bytes(content)  # raw, untouched
    log.info("Saved raw CDC file -> %s (%s bytes, ~%s records)",
             path.name, len(content), record_count)

    manifest = {
        "dataset": "CDC tick surveillance -- Amblyomma americanum established counties",
        "established_through_year": config.CDC_ESTABLISHED_THROUGH_YEAR,
        "source_url": url,
        "landing_page": config.CDC_LANDING_PAGE,
        "pull_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bytes": len(content),
        "record_count": record_count,
        "raw_file": path.name,
        "http_content_type": resp.headers.get("Content-Type"),
        "note": (
            "CDC 'established' status is sticky/monotonic -> cumulative footprint, "
            "not an annual snapshot. Raw file saved untouched."
        ),
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Wrote provenance manifest: %s", manifest_path.name)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CDC Stage 1 acquisition.")
    parser.add_argument("--url", default=None, help="Direct dataset URL override.")
    args = parser.parse_args(argv)

    session = build_session()
    url = args.url or discover_csv_url(session)
    if not url:
        log.error(
            "No dataset URL. Open %s, copy the Amblyomma americanum established-"
            "counties data link, and re-run: python src/acquire_cdc.py --url <link>",
            config.CDC_LANDING_PAGE,
        )
        return 2
    download(url, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
