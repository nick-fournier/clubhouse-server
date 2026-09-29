#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""prep-osrm.py — build the OSRM routing graphs under thinkbox/data/osrm.

The analog of prep-motis.py, for OSRM. It makes the (multi-GB,
gitignored) graphs REPRODUCIBLE from one source extract and the stock profiles:

  for each profile (car, bicycle, foot):
    osrm-extract -> osrm-contract   (CH pipeline, via Docker)
    into data/osrm/<profile>/network.osrm, which compose.yaml serves.

We build **Contraction Hierarchies** (CH), not MLD: this backend never applies
live traffic updates, so CH's faster queries win and we skip partition/customize.
CH's cost is a heavier `osrm-contract` step — see "Memory" below.

The OSRM image is read from compose.yaml, so graphs are always built with the
version the server runs. A profile already built with that image is skipped;
changing the image in compose.yaml makes the next run rebuild it.

The default source is the whole-US Geofabrik extract (~11GB download). The output
is named `network.osrm` regardless of the source file, so compose.yaml serves a
stable path no matter what `--source` you build from.

Memory: at whole-US scale `osrm-contract` peaks well above 16GB, so build this on
a bigger box (the graphs are version-locked but machine-portable — rsync
data/osrm/<profile>/ to thinkbox and serve there mmap'd). Run inside tmux/nohup;
a full build takes hours.

Usage:
  python3 prep-osrm.py                          # build any missing/outdated profile
  python3 prep-osrm.py --profiles car foot      # just these
  python3 prep-osrm.py --force-rebuild          # rebuild even if up to date
  python3 prep-osrm.py --source <path-or-url>   # different .osm.pbf extract

Environment:
  OSRM_DATA_DIR  — output dir (default thinkbox/data/osrm)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen, urlretrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yaml"
DATA_DIR = Path(os.environ.get("OSRM_DATA_DIR", HERE / "data" / "osrm")).resolve()

# Whole-US road network (same extract prep-motis.py uses for street routing).
DEFAULT_SOURCE = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"

# Stock profiles shipped in the OSRM image.
PROFILES = ["car", "bicycle", "foot"]

# Source is linked to this stable name inside each profile dir, so the built
# graph is always network.osrm no matter what the source file is called.
NETWORK_NAME = "network"

# Written into each profile folder after a successful build; holds the image used.
STAMP_FILE = ".built-with"


def osrm_image() -> str:
    """The osrm-backend image compose.yaml serves with (single source of truth)."""
    match = re.search(r"image:\s*(\S*project-osrm/osrm-backend:\S+)", COMPOSE_FILE.read_text())
    if not match:
        sys.exit(f"No osrm-backend image found in {COMPOSE_FILE}")
    return match.group(1)


def verify_md5(path: Path, url: str) -> None:
    """Best-effort integrity check against Geofabrik's <url>.md5 sidecar."""
    try:
        with urlopen(url + ".md5", timeout=30) as resp:
            expected = resp.read().decode().split()[0]
    except Exception as e:  # no sidecar / offline — skip rather than block the build
        logger.warning("Could not fetch %s.md5 (%s); skipping checksum", url, e)
        return
    logger.info("Verifying md5 of %s", path.name)
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        sys.exit(f"md5 mismatch for {path} (got {h.hexdigest()}, expected {expected}); "
                 "delete it and re-run to re-download")
    logger.info("md5 OK")


def fetch_source(source: str) -> Path:
    """Return a local path to the extract, downloading it into DATA_DIR if a URL."""
    if not source.startswith(("http://", "https://")):
        path = Path(source).resolve()
        if not path.is_file():
            sys.exit(f"Source extract not found: {path}")
        return path

    path = DATA_DIR / Path(source.split("?")[0]).name
    if not path.is_file():
        logger.info("Downloading %s (this is large; ~11GB for whole-US)", source)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        urlretrieve(source, tmp)
        tmp.rename(path)  # only publish a complete download
        verify_md5(path, source)
    return path


def resource_limits() -> tuple[int, int]:
    """(threads, memory bytes) for build containers, leaving the host usable.

    One core is left free and the container is capped at 90% of the memory
    available at start, so sshd and friends keep running even when the build
    spills into swap (the container may still use host swap past the cap).
    """
    threads = max(1, (os.cpu_count() or 2) - 1)
    meminfo = Path("/proc/meminfo").read_text()
    available_kb = int(re.search(r"MemAvailable:\s+(\d+) kB", meminfo).group(1))
    return threads, int(available_kb * 1024 * 0.9)


def is_built(profile_dir: Path, image: str) -> bool:
    stamp = profile_dir / STAMP_FILE
    return stamp.is_file() and stamp.read_text().strip() == image


def build_profile(profile: str, source: Path, image: str) -> None:
    profile_dir = DATA_DIR / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / STAMP_FILE).unlink(missing_ok=True)

    # Hard-link the extract into the profile folder under a stable name (no extra
    # disk) so this profile's .osrm files land next to it as network.osrm*,
    # separate from the other profiles and independent of the source filename.
    pbf = profile_dir / f"{NETWORK_NAME}.osm.pbf"
    pbf.unlink(missing_ok=True)
    try:
        os.link(source, pbf)
    except OSError:
        shutil.copy2(source, pbf)

    threads, memory = resource_limits()
    osrm = f"/data/{profile}/{NETWORK_NAME}.osrm"
    steps = [
        ["osrm-extract", "-t", str(threads), "-p", f"/opt/{profile}.lua", f"/data/{profile}/{pbf.name}"],
        ["osrm-contract", "-t", str(threads), osrm],
    ]
    for step in steps:
        logger.info("[%s] %s (cpus %d, memory cap %.1f GiB)", profile, " ".join(step),
                    threads, memory / 2**30)
        started = time.monotonic()
        subprocess.run(
            ["docker", "run", "--rm",
             "--cpus", str(threads), "--memory", str(memory), "--memory-swap", "-1",
             "-v", f"{DATA_DIR}:/data", image, *step],
            check=True,
        )
        logger.info("[%s] %s done in %.0f min", profile, step[0], (time.monotonic() - started) / 60)

    (profile_dir / STAMP_FILE).write_text(image + "\n")
    logger.info("[%s] built %s", profile, osrm)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the OSRM graphs served by thinkbox/compose.yaml.",
    )
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=PROFILES,
                        help="profiles to build (default: all)")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="source .osm.pbf, local path or URL (default: whole-US Geofabrik)")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="rebuild even if a profile is already built with this image")
    args = parser.parse_args()

    image = osrm_image()
    source = fetch_source(args.source)
    logger.info("OSRM image %s, source %s", image, source)

    for profile in args.profiles:
        if not args.force_rebuild and is_built(DATA_DIR / profile, image):
            logger.info("[%s] up to date, skipping (use --force-rebuild to redo)", profile)
            continue
        try:
            build_profile(profile, source, image)
        except subprocess.CalledProcessError as e:
            sys.exit(f"[{profile}] build failed (exit {e.returncode}); see the log above")

    logger.info("All done. Start or restart the backends: docker compose up -d")


if __name__ == "__main__":
    main()
