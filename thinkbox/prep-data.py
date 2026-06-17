#!/usr/bin/env python3
"""prep-data.py — build the MOTIS dataset under ./data for the thinkbox server.

The analog of orange/prep-data.sh, but for MOTIS instead of OSRM, and covering
ALL of the US. It makes the (multi-GB, gitignored) MOTIS import REPRODUCIBLE:

  1. download — discover + fetch US GTFS feeds (Mobility Database) and the
                full-US OpenStreetMap extract from Geofabrik.
  2. import   — sanitize feeds, shift expired calendars onto the timetable
                window, write a routing-only config.yml, and run `motis import`
                via Docker. The result is served by compose.yaml.

Routing-only profile (fits the 16GB box): street_routing ON, but geocoding /
reverse_geocoding / tiles OFF — the address index is the big resident-memory
hog and thinkbox is a pure routing backend (clients send coordinates).

Stdlib only, except one dependency: `timezonefinder`, managed by uv (see
pyproject.toml / uv.lock). Run via `uv run prep-data.py` so it executes in the
project venv. When a feed omits the GTFS-required agency_timezone, it's inferred
from a representative stop coordinate and injected; if timezonefinder is somehow
unavailable those feeds are dropped instead. No numpy/scipy/h3/py-motis.

Usage:
  uv run prep-data.py                   # full pipeline (download + import)
  uv run prep-data.py --download-only   # just fetch GTFS + OSM
  uv run prep-data.py --num-days 30     # timetable window (default 30)
  uv run prep-data.py --date 2026-06-16 # override reference date (a Monday)
  uv run prep-data.py --force-rebuild   # re-import even if a dataset exists

Environment (thinkbox/.env, gitignored):
  MOBILITY_DB_REFRESH_TOKEN  — register free at https://mobilitydatabase.org
  MOTIS_TAG                  — MOTIS image tag (default 2.8.3)
  NUM_DAYS                   — timetable window if --num-days not given
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, urlretrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Lift csv's default 128 KB field cap so legitimately large fields (long HTML
# descriptions, etc.) parse — but keep it bounded so a pathological unterminated
# quote that slurps an entire table still errors out and gets the feed dropped,
# rather than silently merging rows into one garbage cell.
csv.field_size_limit(4 * 1024 * 1024)  # 4 MB


# ============================================================================
# Constants
# ============================================================================

GEOFABRIK_US_URL = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"
OSM_FILENAME = "us-latest.osm.pbf"
MOBILITY_DB_TOKEN_URL = "https://api.mobilitydatabase.org/v1/tokens"
MOBILITY_DB_FEEDS_URL = "https://api.mobilitydatabase.org/v1/gtfs_feeds"

MOTIS_IMAGE = "ghcr.io/motis-project/motis"
MOTIS_TAG = os.environ.get("MOTIS_TAG", "2.8.3")

DATA_DIR = Path(__file__).resolve().parent / "data"
GTFS_DIR = DATA_DIR / "gtfs"
USER_AGENT = "thinkbox-motis/1.0"

# Some agencies block default clients (403) or have expired TLS certs. For the
# ~3% of feeds with no Mobility-hosted mirror we fall back to the agency URL and
# retry with a browser UA / unverified TLS — it's a public GTFS zip, not an
# authenticated endpoint, so cert verification buys little here.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE

REQUIRED_TABLES = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
# `motis import` writes its compiled dataset here; presence means "imported".
IMPORT_MARKER = DATA_DIR / "data" / "meta" / "tt.json"


# ============================================================================
# .env loading
# ============================================================================

def _load_dotenv(path: Path | None = None) -> None:
    """Load key=value pairs from a .env file into ``os.environ``."""
    env_path = path or (Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        return
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if value and not os.environ.get(key):
                os.environ[key] = value


# ============================================================================
# Phase 1: GTFS + OSM acquisition
# ============================================================================

def _http_get_json(url: str, headers: dict | None = None, timeout: int = 60):
    """GET a URL and parse as JSON."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def query_mobility_db() -> list[dict[str, str]]:
    """Query the Mobility Database API for ALL US GTFS feeds.

    Requires ``MOBILITY_DB_REFRESH_TOKEN``. Register free at
    https://mobilitydatabase.org. Returns ``{"name", "url"}`` dicts.
    """
    refresh_token = os.environ.get("MOBILITY_DB_REFRESH_TOKEN")
    if not refresh_token:
        logger.warning(
            "MOBILITY_DB_REFRESH_TOKEN not set — cannot discover US feeds.\n"
            "  Register free at https://mobilitydatabase.org and set it in .env,\n"
            "  or drop GTFS .zip files into %s manually.",
            GTFS_DIR,
        )
        return []

    logger.info("Authenticating with Mobility Database API...")
    req = Request(
        MOBILITY_DB_TOKEN_URL,
        data=json.dumps({"refresh_token": refresh_token}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        access_token = json.loads(resp.read())["access_token"]

    logger.info("Querying Mobility Database for US GTFS feeds...")
    feeds: list[dict[str, str]] = []
    offset = 0
    limit = 100

    while True:
        url = (
            f"{MOBILITY_DB_FEEDS_URL}"
            f"?country_code=US&limit={limit}&offset={offset}"
        )
        data = _http_get_json(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
        if not data:
            break

        for feed in data:
            provider = feed.get("provider", "")
            if isinstance(provider, dict):
                name = provider.get("name", "unknown")
            else:
                name = str(provider) or feed.get("feed_name", "unknown")

            latest = feed.get("latest_dataset") or {}
            durl = (
                latest.get("hosted_url")
                or latest.get("download_url")
                or latest.get("url")
                or ""
            )
            if not durl:
                source = feed.get("source_info") or {}
                durl = source.get("producer_url", "")

            if durl:
                feeds.append({"name": name, "url": durl})

        if len(data) < limit:
            break
        offset += limit

    logger.info("Mobility Database: found %d US GTFS feeds", len(feeds))
    return feeds


def discover_feeds() -> list[dict[str, str]]:
    """Discover all US GTFS feed URLs (deduplicated by URL)."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for feed in query_mobility_db():
        if feed["url"] not in seen:
            seen.add(feed["url"])
            out.append(feed)
    logger.info("Total: %d unique GTFS feeds discovered", len(out))
    return out


def _download_one(url: str, dest: Path) -> tuple[str, bool, str]:
    """Download a single file with tiered retries. Returns ``(url, ok, msg)``.

    Strategies, in order: default UA + verified TLS, then browser UA (for 403s),
    then browser UA + unverified TLS (for expired/invalid agency certs). A 404 or
    other hard error stops early — no point retrying a dead link.
    """
    last = "unknown error"
    for ua, ctx in (
        (USER_AGENT, None),
        (BROWSER_UA, None),
        (BROWSER_UA, _UNVERIFIED_SSL),
    ):
        try:
            req = Request(url, headers={"User-Agent": ua})
            with urlopen(req, timeout=120, context=ctx) as resp:
                data = resp.read()
            dest.write_bytes(data)
            if not zipfile.is_zipfile(dest):
                dest.unlink()
                return url, False, "not a valid zip"
            return url, True, f"{len(data) / 1e6:.1f} MB"
        except HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 429):
                continue  # try a browser UA
            break  # 404 / 5xx — don't bother retrying
        except URLError as e:
            last = str(e.reason)
            if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE" in str(e.reason).upper():
                continue  # try unverified TLS
            break
        except (TimeoutError, OSError) as e:
            last = str(e)
            break
    if dest.exists():
        dest.unlink()
    return url, False, last


def _progress(done: int, total: int, ok: int, fail: int, width: int = 30) -> None:
    """Render an in-place download progress bar to stderr."""
    frac = done / total if total else 1.0
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(f"\r  [{bar}] {done}/{total}  ok={ok} fail={fail}  ")
    sys.stderr.flush()


def download_gtfs_feeds(force: bool = False, max_workers: int = 8) -> list[Path]:
    """Download all discovered GTFS feeds into ``GTFS_DIR``.

    Returns paths to valid GTFS zips (including pre-existing). If discovery
    yields nothing (no token), falls back to whatever zips are already present.
    """
    GTFS_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(GTFS_DIR.glob("*.zip"))
    feeds = discover_feeds()

    if not feeds:
        valid = [z for z in existing if zipfile.is_zipfile(z)]
        if valid:
            logger.info("Using %d pre-existing GTFS files in %s", len(valid), GTFS_DIR)
            return valid
        raise RuntimeError(
            "No GTFS feeds discovered and none present.\n"
            "  Set MOBILITY_DB_REFRESH_TOKEN in .env, or drop .zip files in "
            f"{GTFS_DIR}"
        )

    to_download: list[tuple[dict, Path]] = []
    for feed in feeds:
        fname = Path(feed["url"].split("?")[0]).name
        if not fname.endswith(".zip"):
            fname = re.sub(r"[^\w.-]", "_", feed["name"])[:80] + ".zip"
        dest = GTFS_DIR / fname
        if dest.exists() and not force:
            continue
        to_download.append((feed, dest))

    cached = len(feeds) - len(to_download)
    if to_download:
        total = len(to_download)
        logger.info("Downloading %d GTFS feeds (%d already cached)...", total, cached)
        ok = fail = done = 0
        failures: list[tuple[str, str]] = []
        # Per-feed failures are collected (not logged inline) so they don't
        # shred the live progress bar; a summary prints at the end.
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(_download_one, f["url"], d): f["name"]
                    for f, d in to_download
                }
                _progress(0, total, 0, 0)
                for fut in as_completed(futures):
                    name = futures[fut]
                    _url, success, msg = fut.result()
                    done += 1
                    if success:
                        ok += 1
                    else:
                        fail += 1
                        failures.append((name, msg))
                    _progress(done, total, ok, fail)
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            logger.error("Interrupted during GTFS download — aborting.")
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        sys.stderr.write("\n")
        logger.info("Downloads: %d succeeded, %d failed", ok, fail)
        if failures:
            logger.info(
                "Skipped %d feeds (dead/blocked agency links, no Mobility mirror):",
                len(failures),
            )
            for name, msg in failures[:15]:
                logger.info("  - %s: %s", name, msg)
            if len(failures) > 15:
                logger.info("  ... and %d more", len(failures) - 15)
    else:
        logger.info("All %d feeds already cached", cached)

    valid = [z for z in sorted(GTFS_DIR.glob("*.zip")) if zipfile.is_zipfile(z)]
    if not valid:
        raise RuntimeError(
            f"No valid GTFS files after download. Check connectivity and "
            f"MOBILITY_DB_REFRESH_TOKEN.\n  Directory: {GTFS_DIR}"
        )
    logger.info("Total valid GTFS files: %d", len(valid))
    return valid


def download_osm_pbf(force: bool = False) -> Path:
    """Download the full-US OSM PBF from Geofabrik (~10-11 GB)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / OSM_FILENAME

    if dest.exists() and not force:
        logger.info("OSM PBF already exists: %s (%.0f MB)", dest, dest.stat().st_size / 1e6)
        return dest

    logger.info("Downloading US OSM PBF from Geofabrik (~10-11 GB)... this is slow.")
    t0 = time.time()
    try:
        urlretrieve(GEOFABRIK_US_URL, dest)
    except BaseException:
        if dest.exists():
            dest.unlink()
        raise
    logger.info(
        "Downloaded OSM PBF: %.0f MB in %.0fs",
        dest.stat().st_size / 1e6, time.time() - t0,
    )
    return dest


# ============================================================================
# Phase 2: GTFS sanitization + date handling
# ============================================================================

def _normalize_table(raw: bytes) -> bytes:
    """Strip BOM + surrounding whitespace from every CSV cell; normalize EOL to LF.

    GTFS forbids surrounding whitespace in fields, but sloppy producers emit
    ``", "``-style delimiters and CRLF line endings. The stray leading space
    turns ``America/Chicago`` into ``" America/Chicago"``, which fails MOTIS's
    strict timezone lookup (and can break other strict parses). Round-tripping
    through ``csv`` preserves quoting/embedded commas while trimming each cell.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    # skipinitialspace honors quotes that follow a ", " delimiter (so an embedded
    # comma in a quoted field isn't split); .strip() handles trailing whitespace.
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        writer.writerow([c.strip() for c in row])
    return buf.getvalue().encode("utf-8")


# Lazy singleton — TimezoneFinder loads boundary data once and is reused.
_TZ_FINDER = None
_TZ_FINDER_TRIED = False


def _lookup_timezone(lat: float, lon: float) -> str | None:
    """Return the IANA timezone at (lat, lon), or None.

    Uses the optional ``timezonefinder`` package (offline boundary polygons).
    If it isn't installed, warns once and returns None so the caller drops the
    feed rather than crashing the whole run.
    """
    global _TZ_FINDER, _TZ_FINDER_TRIED
    if not _TZ_FINDER_TRIED:
        _TZ_FINDER_TRIED = True
        try:
            from timezonefinder import TimezoneFinder
            _TZ_FINDER = TimezoneFinder()
        except ImportError:
            logger.warning(
                "timezonefinder not installed — cannot infer missing "
                "agency_timezone; such feeds will be dropped. "
                "Install it: pip install timezonefinder"
            )
    if _TZ_FINDER is None:
        return None
    try:
        return _TZ_FINDER.timezone_at(lat=lat, lng=lon)
    except Exception:
        return None


def _representative_coord(
    zin: zipfile.ZipFile, file_map: dict[str, str]
) -> tuple[float, float] | None:
    """Per-axis median (lat, lon) of a feed's valid stops, or None."""
    if "stops.txt" not in file_map:
        return None
    text = _normalize_table(zin.read(file_map["stops.txt"])).decode("utf-8")
    lats: list[float] = []
    lons: list[float] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            la = float(row["stop_lat"])
            lo = float(row["stop_lon"])
        except (KeyError, ValueError, TypeError):
            continue
        if -90 <= la <= 90 and -180 <= lo <= 180 and not (la == 0 and lo == 0):
            lats.append(la)
            lons.append(lo)
    if not lats:
        return None
    lats.sort()
    lons.sort()
    mid = len(lats) // 2
    return lats[mid], lons[mid]


def _inject_timezone(agency_text: str, tz: str) -> bytes:
    """Return agency.txt bytes with ``agency_timezone`` filled in with *tz*.

    Adds the column if absent; fills only empty values so any present zones are
    preserved. *agency_text* is the already-normalized table text.
    """
    reader = csv.DictReader(io.StringIO(agency_text))
    fields = list(reader.fieldnames or [])
    if "agency_timezone" not in fields:
        fields.append("agency_timezone")
    rows = []
    for r in reader:
        if not (r.get("agency_timezone") or "").strip():
            r["agency_timezone"] = tz
        rows.append(r)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (r.get(k) or "") for k in fields})
    return buf.getvalue().encode("utf-8")


def _sanitize_gtfs(gtfs_files: list[Path], output_dir: Path) -> list[Path]:
    """Sanitize GTFS zips for MOTIS compatibility.

    - Flattens files nested in subdirectories to the zip root.
    - Strips header-only (empty) optional tables.
    - Normalizes CSV whitespace/BOM/EOL in every table (see _normalize_table).
    - Drops feeds missing required tables or ``agency_timezone``.
    Returns paths to sanitized copies (or cached copies from a prior run).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    result: list[Path] = []
    sanitized = dropped = inferred = 0
    total = len(gtfs_files)
    last_log = time.time()
    logger.info("Sanitizing %d GTFS feeds...", total)

    for i, gtfs_path in enumerate(gtfs_files):
        if time.time() - last_log >= 5.0:
            last_log = time.time()
            logger.info("  sanitize %d/%d (kept %d, dropped %d)", i, total, len(result), dropped)
        out_path = output_dir / gtfs_path.name
        if out_path.exists():
            if zipfile.is_zipfile(out_path):
                result.append(out_path)
                continue
            out_path.unlink()  # partial/corrupt from an aborted run — redo it
        try:
            with zipfile.ZipFile(gtfs_path, "r") as zin:
                names = zin.namelist()

                file_map: dict[str, str] = {}
                for n in names:
                    basename = n.rsplit("/", 1)[-1] if "/" in n else n
                    if basename.endswith(".txt") and basename not in file_map:
                        file_map[basename] = n

                missing = REQUIRED_TABLES - set(file_map.keys())
                if missing:
                    logger.warning(
                        "Dropping %s: missing required tables %s",
                        gtfs_path.name, sorted(missing),
                    )
                    dropped += 1
                    continue

                # MOTIS requires agency_timezone. Parse the (normalized) agency
                # table with csv so quoted fields / embedded commas don't throw
                # off the column count the way a naive split would.
                agency_text = _normalize_table(zin.read(file_map["agency.txt"])).decode("utf-8")
                reader = csv.DictReader(io.StringIO(agency_text))
                fields = reader.fieldnames or []
                tz_values = [(r.get("agency_timezone") or "").strip() for r in reader]
                agency_override: bytes | None = None
                if "agency_timezone" not in fields or not any(tz_values):
                    # Infer the zone from a representative stop coordinate and
                    # inject it, rather than dropping the feed.
                    coord = _representative_coord(zin, file_map)
                    tz = _lookup_timezone(*coord) if coord else None
                    if not tz:
                        logger.warning(
                            "Dropping %s: no agency_timezone and could not infer one",
                            gtfs_path.name,
                        )
                        dropped += 1
                        continue
                    agency_override = _inject_timezone(agency_text, tz)
                    inferred += 1
                    logger.debug("Inferred agency_timezone=%s for %s", tz, gtfs_path.name)

                empty_tables: set[str] = set()
                for basename, zip_path in file_map.items():
                    tdata = zin.read(zip_path).decode("utf-8", errors="replace").strip()
                    lines = tdata.split("\n")
                    if len(lines) <= 1 and lines[0].strip():
                        empty_tables.add(zip_path)

                if empty_tables:
                    empty_basenames = {
                        zp.rsplit("/", 1)[-1] if "/" in zp else zp for zp in empty_tables
                    }
                    empty_required = REQUIRED_TABLES & empty_basenames
                    if empty_required:
                        logger.warning(
                            "Dropping %s: required tables are empty: %s",
                            gtfs_path.name, sorted(empty_required),
                        )
                        dropped += 1
                        continue

                # Always rewrite: flatten nested paths to root, drop empty
                # optional tables, and normalize CSV whitespace/BOM/EOL.
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
                    for basename, zip_path in file_map.items():
                        if zip_path in empty_tables:
                            continue
                        if basename == "agency.txt" and agency_override is not None:
                            zout.writestr(basename, agency_override)
                        else:
                            zout.writestr(basename, _normalize_table(zin.read(zip_path)))
                result.append(out_path)
                sanitized += 1
        except zipfile.BadZipFile:
            logger.warning("Dropping %s: corrupt zip", gtfs_path.name)
            if out_path.exists():
                out_path.unlink()
            dropped += 1
            continue
        except Exception as e:
            # One malformed feed (bad CSV quoting, oversized field, etc.) must
            # never abort a 1600-feed run — drop it and move on. Remove any
            # partial output so the next run's cache check doesn't reuse it.
            logger.warning("Dropping %s: sanitize failed (%s)", gtfs_path.name, e)
            if out_path.exists():
                out_path.unlink()
            dropped += 1
            continue

    if sanitized or dropped:
        logger.info(
            "GTFS sanitization: %d normalized (%d tz-inferred), %d dropped, %d cached",
            sanitized, inferred, dropped, len(result) - sanitized,
        )
    return result


def _get_feed_end_date(zf: zipfile.ZipFile) -> date | None:
    """Return the latest service end date from a GTFS zip."""
    max_date: date | None = None
    if "calendar.txt" in zf.namelist():
        with zf.open("calendar.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
            for row in reader:
                try:
                    end = datetime.strptime(row["end_date"], "%Y%m%d").date()
                    if max_date is None or end > max_date:
                        max_date = end
                except (ValueError, KeyError):
                    pass
    if max_date is None and "calendar_dates.txt" in zf.namelist():
        with zf.open("calendar_dates.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
            for row in reader:
                try:
                    d = datetime.strptime(row["date"], "%Y%m%d").date()
                    if max_date is None or d > max_date:
                        max_date = d
                except (ValueError, KeyError):
                    pass
    return max_date


def _shift_date(d: date, weeks: int) -> str:
    """Shift a date forward by N weeks; return YYYYMMDD."""
    return (d + timedelta(weeks=weeks)).strftime("%Y%m%d")


def shift_expired_feeds(
    gtfs_files: list[Path], target_date: date, output_dir: Path,
) -> list[Path]:
    """Shift calendar dates of expired feeds so they cover ``target_date``.

    For each feed whose service ends before ``target_date``, rewrites
    ``calendar.txt`` / ``calendar_dates.txt`` shifting all dates forward by
    whole weeks (preserving day-of-week). Active feeds pass through unchanged.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    result: list[Path] = []
    shifted = 0
    total = len(gtfs_files)
    last_log = time.time()
    DATE_TABLES = {"calendar.txt", "calendar_dates.txt"}
    DATE_COLS_CALENDAR = {"start_date", "end_date"}
    DATE_COLS_DATES = {"date"}

    for i, gtfs_path in enumerate(gtfs_files):
        if time.time() - last_log >= 5.0:
            last_log = time.time()
            logger.info("  date-shift %d/%d (shifted %d)", i, total, shifted)
        try:
            with zipfile.ZipFile(gtfs_path, "r") as zf:
                end_date = _get_feed_end_date(zf)
                if end_date is None or end_date >= target_date:
                    result.append(gtfs_path)
                    continue

                days_short = (target_date - end_date).days
                weeks_shift = (days_short // 7) + 1

                out_path = output_dir / gtfs_path.name
                if out_path.exists():
                    result.append(out_path)
                    shifted += 1
                    continue

                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
                    for item in zf.infolist():
                        data = zf.read(item.filename)
                        basename = item.filename.rsplit("/", 1)[-1] if "/" in item.filename else item.filename

                        if basename in DATE_TABLES:
                            text = data.decode("utf-8-sig", errors="replace")
                            lines = text.strip().split("\n")
                            if len(lines) < 2:
                                zout.writestr(item.filename, data)
                                continue
                            header = lines[0]
                            cols = [c.strip() for c in header.split(",")]
                            date_cols = DATE_COLS_CALENDAR if basename == "calendar.txt" else DATE_COLS_DATES
                            date_idxs = [i for i, c in enumerate(cols) if c in date_cols]
                            if not date_idxs:
                                zout.writestr(item.filename, data)
                                continue
                            new_lines = [header]
                            for line in lines[1:]:
                                if not line.strip():
                                    continue
                                fields = line.split(",")
                                for idx in date_idxs:
                                    if idx < len(fields):
                                        raw = fields[idx].strip()
                                        try:
                                            d = datetime.strptime(raw, "%Y%m%d").date()
                                            fields[idx] = _shift_date(d, weeks_shift)
                                        except ValueError:
                                            pass
                                new_lines.append(",".join(fields))
                            zout.writestr(item.filename, "\n".join(new_lines) + "\n")
                        else:
                            zout.writestr(item, data)

                shifted += 1
                result.append(out_path)
        except zipfile.BadZipFile:
            logger.warning("Skipping corrupt zip: %s", gtfs_path.name)
            continue

    if shifted:
        logger.info(
            "Date-shifted %d / %d expired feeds to cover %s",
            shifted, len(gtfs_files), target_date,
        )
    return result


# ============================================================================
# Phase 3: MOTIS config + import
# ============================================================================

def write_motis_config(
    data_dir: Path,
    osm_file: str,
    gtfs_files: list[Path],
    first_day: date,
    num_days: int,
) -> Path:
    """Write a routing-only ``config.yml`` for ``motis import``.

    street_routing ON; geocoding / reverse_geocoding / tiles OFF so the dataset
    fits a 16GB box (the address index is the big resident-memory consumer).
    """
    # MOTIS dataset identifiers must be alphanumeric/dash; dedup on collision.
    datasets: dict[str, str] = {}
    for gtfs in gtfs_files:
        name = re.sub(r"[^a-zA-Z0-9-]", "-", gtfs.stem)
        base, i = name, 2
        while name in datasets:
            name = f"{base}-{i}"
            i += 1
        datasets[name] = gtfs.name

    lines = [
        f"osm: {osm_file}",
        "street_routing: true",
        "geocoding: false",
        "reverse_geocoding: false",
        "timetable:",
        f"  first_day: {first_day.isoformat()}",
        f"  num_days: {num_days}",
        "  datasets:",
    ]
    for ds_name, ds_path in datasets.items():
        lines.append(f"    {ds_name}:")
        lines.append(f"      path: {ds_path}")

    config_path = data_dir / "config.yml"
    config_path.write_text("\n".join(lines) + "\n")
    logger.info(
        "Wrote %s (%d datasets, first_day=%s, num_days=%d, routing-only)",
        config_path, len(datasets), first_day, num_days,
    )
    return config_path


def run_import(data_dir: Path, tag: str = MOTIS_TAG) -> None:
    """Run ``motis import`` in Docker against ``data_dir/config.yml``."""
    image = f"{MOTIS_IMAGE}:{tag}"
    logger.info("Pulling %s ...", image)
    subprocess.run(["docker", "pull", image], check=True)

    logger.info("Running `motis import` — this may take 30+ min and peak several GB RAM.")
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{data_dir}:/data",
        "-w", "/data",
        "--user", "root",
        "--entrypoint", "/motis",
        image, "import",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        # Surface MOTIS's own verification failures, if any.
        tt_log = data_dir / "data" / "logs" / "tt.txt"
        if tt_log.exists():
            fails = [ln.strip() for ln in tt_log.read_text().splitlines() if "VERIFY FAIL" in ln]
            if fails:
                logger.error("MOTIS import failed. VERIFY FAIL entries:\n  %s", "\n  ".join(fails[-5:]))
        raise
    logger.info("MOTIS import complete: %s", IMPORT_MARKER)


def _link_or_copy(src: Path, dest: Path) -> None:
    """Hardlink src→dest (fall back to copy across filesystems)."""
    if dest.exists():
        return
    try:
        os.link(src.resolve(), dest)
    except OSError:
        import shutil
        shutil.copy2(src, dest)


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Build the MOTIS dataset under ./data (all-US, routing-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--download-only", action="store_true",
                        help="Only download GTFS + OSM (skip import)")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Re-import even if a dataset already exists")
    parser.add_argument("--force-download", action="store_true",
                        help="Re-download GTFS + OSM even if cached")
    parser.add_argument("--num-days", type=int,
                        default=int(os.environ.get("NUM_DAYS", "30")),
                        help="Timetable window in days (default 30)")
    parser.add_argument("--date", type=str, default=None,
                        help="Reference date YYYY-MM-DD (first_day = its Monday)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Phase 1: download ----
    print("\n" + "#" * 60 + "\n# Phase 1: download GTFS + US OSM\n" + "#" * 60)
    gtfs_files = download_gtfs_feeds(force=args.force_download)
    osm_path = download_osm_pbf(force=args.force_download)

    if args.download_only:
        print(f"\nDownload complete: {len(gtfs_files)} GTFS feeds + {osm_path}")
        return

    # ---- Reference date / timetable window ----
    if args.date:
        ref = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        ref = date.today()
    first_day = ref - timedelta(days=ref.weekday())  # Monday of ref week

    if IMPORT_MARKER.exists() and not args.force_rebuild:
        logger.info("Dataset already imported (%s). Use --force-rebuild to redo.", IMPORT_MARKER)
        print("\nDataset present. Start the server with: docker compose up -d")
        return

    # ---- Phase 2: sanitize + shift ----
    print("\n" + "#" * 60 + "\n# Phase 2: sanitize + date-shift feeds\n" + "#" * 60)
    clean = _sanitize_gtfs(gtfs_files, DATA_DIR / "_sanitized")
    clean = shift_expired_feeds(clean, first_day, DATA_DIR / "_shifted")

    # Flatten feeds + OSM into the data dir root that MOTIS imports from.
    staged: list[Path] = []
    for feed in clean:
        dest = DATA_DIR / feed.name
        _link_or_copy(feed, dest)
        staged.append(dest)
    osm_dest = DATA_DIR / OSM_FILENAME
    _link_or_copy(osm_path, osm_dest)

    # ---- Phase 3: config + import ----
    print("\n" + "#" * 60 + "\n# Phase 3: write config + motis import\n" + "#" * 60)
    write_motis_config(DATA_DIR, OSM_FILENAME, staged, first_day, args.num_days)
    run_import(DATA_DIR)

    print("\nImport complete. Start the server with: docker compose up -d")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user — exiting.", file=sys.stderr)
        sys.exit(130)
