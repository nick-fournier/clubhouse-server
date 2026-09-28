#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""prep-osrm.py — build the OSRM routing graphs under thinkbox/data/osrm.

The analog of prep-motis.py, for OSRM. It makes the (multi-GB,
gitignored) graphs REPRODUCIBLE from one source extract and the stock profiles:

  for each profile (car, bicycle, foot):
    osrm-extract -> osrm-partition -> osrm-customize   (MLD pipeline, via Docker)
    into data/osrm/<profile>/cropped_network.osrm, which compose.yaml serves.

The OSRM image is read from compose.yaml, so graphs are always built with the
version the server runs. A profile already built with that image is skipped;
changing the image in compose.yaml makes the next run rebuild it.

The default source, data/osrm/cropped_network.osm.pbf, was cut to a boundary
with osmium (see nick-fournier/GraphSeq scripts/helpers/prepare_osrm.py).

Stdlib only. Builds are memory-heavy: stop MOTIS first on the 16GB box, and run
inside tmux (or nohup) since a full build takes hours.

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
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlretrieve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yaml"
DATA_DIR = Path(os.environ.get("OSRM_DATA_DIR", HERE / "data" / "osrm")).resolve()
DEFAULT_SOURCE = DATA_DIR / "cropped_network.osm.pbf"

# Stock profiles shipped in the OSRM image.
PROFILES = ["car", "bicycle", "foot"]

# Written into each profile folder after a successful build; holds the image used.
STAMP_FILE = ".built-with"


def osrm_image() -> str:
    """The osrm-backend image compose.yaml serves with (single source of truth)."""
    match = re.search(r"image:\s*(\S*project-osrm/osrm-backend:\S+)", COMPOSE_FILE.read_text())
    if not match:
        sys.exit(f"No osrm-backend image found in {COMPOSE_FILE}")
    return match.group(1)


def fetch_source(source: str) -> Path:
    """Return a local path to the extract, downloading it into DATA_DIR if a URL."""
    if not source.startswith(("http://", "https://")):
        path = Path(source).resolve()
        if not path.is_file():
            sys.exit(f"Source extract not found: {path}")
        return path

    path = DATA_DIR / Path(source.split("?")[0]).name
    if not path.is_file():
        logger.info("Downloading %s", source)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        urlretrieve(source, path)
    return path


def is_built(profile_dir: Path, image: str) -> bool:
    stamp = profile_dir / STAMP_FILE
    return stamp.is_file() and stamp.read_text().strip() == image


def build_profile(profile: str, source: Path, image: str) -> None:
    profile_dir = DATA_DIR / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / STAMP_FILE).unlink(missing_ok=True)

    # Hard-link the extract into the profile folder (no extra disk) so this
    # profile's .osrm files land next to it, separate from the other profiles.
    pbf = profile_dir / source.name
    pbf.unlink(missing_ok=True)
    try:
        os.link(source, pbf)
    except OSError:
        shutil.copy2(source, pbf)

    osrm = f"/data/{profile}/{pbf.name.removesuffix('.osm.pbf')}.osrm"
    steps = [
        ["osrm-extract", "-p", f"/opt/{profile}.lua", f"/data/{profile}/{pbf.name}"],
        ["osrm-partition", osrm],
        ["osrm-customize", osrm],
    ]
    for step in steps:
        logger.info("[%s] %s", profile, " ".join(step))
        started = time.monotonic()
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{DATA_DIR}:/data", image, *step],
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
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="source .osm.pbf, local path or URL (default: %(default)s)")
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
