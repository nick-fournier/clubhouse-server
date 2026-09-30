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

The default source is the Geofabrik US West extract (~3-4GB download): whole-US
needs >100GB of RAM + swap to extract (see "Memory" below), and at that size
the build thrashes swap for days on a 48GB box. The output
is named `network.osrm` regardless of the source file, so compose.yaml serves a
stable path no matter what `--source` you build from.

Memory: even for US West, extract and contract peak well above 16GB, so build on
a bigger box (the graphs are version-locked but machine-portable — rsync
data/osrm/<profile>/ to thinkbox and serve there mmap'd). Run inside tmux/nohup;
a full build takes hours.

Memory (measured on cube, 48GB RAM): whole-US car osrm-extract needs >100GB of
RAM + swap (41.6GiB RAM + 55.7GiB swap and still growing), and with most of that
in swap it slowed to ~2% CPU, so steps took ~20-50x longer. That is why the
default is US West. If the osrm-build.slice unit (next to this
script) is installed, builds run under it: MemoryHigh makes the build spill to
swap past ~40GB instead of starving the host, and never kills it. Without the
slice, memory is uncapped. Either way the container gets oom_score_adj 1000, so
if RAM + swap really run out the kernel kills the build, not sshd. Install with:
  sudo cp osrm-build.slice /etc/systemd/system/ && sudo systemctl daemon-reload

Usage:
  python3 prep-osrm.py                          # build any missing/outdated profile
  python3 prep-osrm.py --profiles car foot      # just these
  python3 prep-osrm.py --force-rebuild          # rebuild even if up to date
  python3 prep-osrm.py --source <path-or-url>   # different .osm.pbf extract
  python3 prep-osrm.py --profiles car --skip-extract  # contract an existing extract

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

# US West road network. Whole-US (north-america/us-latest.osm.pbf, the extract
# prep-motis.py uses) is too big to build on a 48GB box; see "Memory" above.
DEFAULT_SOURCE = "https://download.geofabrik.de/north-america/us-west-latest.osm.pbf"

# Stock profiles shipped in the OSRM image.
PROFILES = ["car", "bicycle", "foot"]

# Source is linked to this stable name inside each profile dir, so the built
# graph is always network.osrm no matter what the source file is called.
NETWORK_NAME = "network"

# Written into each profile folder after a successful build; holds the image used.
STAMP_FILE = ".built-with"

# systemd slice that soft-caps build memory (see "Memory" above); used if installed.
BUILD_SLICE = "osrm-build.slice"

# Fixed container name: a second concurrent build fails fast instead of racing
# the first one for RAM + swap.
CONTAINER_NAME = "osrm-build"


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
        logger.info("Downloading %s (this is large: ~3-4GB for US West, ~11GB for whole-US)", source)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        urlretrieve(source, tmp)
        tmp.rename(path)  # only publish a complete download
        verify_md5(path, source)
    return path


def build_threads() -> int:
    """Threads for build containers: all cores but one, so the host stays responsive."""
    return max(1, (os.cpu_count() or 2) - 1)


def build_slice_installed() -> bool:
    """True if BUILD_SLICE has a unit file (systemd would otherwise create an unlimited one)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "FragmentPath", "--value", BUILD_SLICE],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(out)


def is_built(profile_dir: Path, image: str) -> bool:
    stamp = profile_dir / STAMP_FILE
    return stamp.is_file() and stamp.read_text().strip() == image


def build_profile(profile: str, source: Path, image: str, skip_extract: bool = False) -> None:
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

    threads = build_threads()
    osrm = f"/data/{profile}/{NETWORK_NAME}.osrm"
    steps = [
        ["osrm-extract", "-t", str(threads), "-p", f"/opt/{profile}.lua", f"/data/{profile}/{pbf.name}"],
        ["osrm-contract", "-t", str(threads), osrm],
    ]
    if skip_extract:
        steps = steps[1:]
    # --init: osrm-* as PID 1 ignores SIGINT/SIGTERM, so without it Ctrl-C kills
    # the docker client but leaves the build running as an orphan.
    run_opts = ["--rm", "--init", "--name", CONTAINER_NAME,
                "--cpus", str(threads), "--oom-score-adj", "1000"]
    if build_slice_installed():
        run_opts += ["--cgroup-parent", BUILD_SLICE]
        logger.info("[%s] running under %s (soft memory cap)", profile, BUILD_SLICE)
    else:
        logger.info("[%s] %s not installed; memory uncapped", profile, BUILD_SLICE)
    for step in steps:
        logger.info("[%s] %s (cpus %d)", profile, " ".join(step), threads)
        started = time.monotonic()
        subprocess.run(
            ["docker", "run", *run_opts,
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
                        help="source .osm.pbf, local path or URL (default: Geofabrik US West)")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="rebuild even if a profile is already built with this image")
    parser.add_argument("--skip-extract", action="store_true",
                        help="only run osrm-contract, on an existing extract in the profile dir")
    args = parser.parse_args()

    image = osrm_image()
    source = fetch_source(args.source)
    logger.info("OSRM image %s, source %s", image, source)

    for profile in args.profiles:
        if not args.force_rebuild and is_built(DATA_DIR / profile, image):
            logger.info("[%s] up to date, skipping (use --force-rebuild to redo)", profile)
            continue
        try:
            build_profile(profile, source, image, args.skip_extract)
        except subprocess.CalledProcessError as e:
            sys.exit(f"[{profile}] build failed (exit {e.returncode}); see the log above")

    logger.info("All done. Start or restart the backends: docker compose up -d")


if __name__ == "__main__":
    main()
