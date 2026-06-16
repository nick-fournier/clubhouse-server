#!/usr/bin/env bash
#
# prep-data.sh — regenerate the OSRM graph files under ./data
#
# The compiled .osrm files are multi-GB and gitignored. This script makes them
# REPRODUCIBLE: given a region .pbf and a profile, it runs the standard OSRM
# pipeline (extract -> partition -> customize) that osrm-routed consumes.
#
# It captures the standard car/bike/foot profiles. The "custom" profiles in this
# stack were built with hand-edited Lua (e.g. bicycle_wrongways.lua) and/or a
# cropped extract that currently live ONLY on orange's disk, not in git. TODO:
# commit those .lua profiles into orange/profiles/ so this script can fully
# rebuild every backend. Until then, the custom_* data is not reproducible here.
#
# Usage:
#   ./prep-data.sh <profile> <pbf-url-or-path> <out-subdir>
# Examples:
#   ./prep-data.sh car  https://download.geofabrik.de/north-america/us/california-latest.osm.pbf  car
#   ./prep-data.sh foot ./data/california-latest.osm.pbf                                           foot
#
set -euo pipefail

PROFILE="${1:?profile required: car|bicycle|foot}"
PBF_SRC="${2:?pbf url or local path required}"
OUT="${3:?output subdir under ./data required}"

OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:v5.27.1"
DATA_DIR="$(cd "$(dirname "$0")" && pwd)/data"
WORK="${DATA_DIR}/${OUT}"
mkdir -p "${WORK}"

# Map our profile name to the built-in Lua profile shipped in the image.
case "${PROFILE}" in
  car)     LUA="/opt/car.lua" ;;
  bicycle) LUA="/opt/bicycle.lua" ;;
  foot)    LUA="/opt/foot.lua" ;;
  *) echo "Unknown profile '${PROFILE}'. For custom profiles, mount your own .lua and edit LUA below." >&2; exit 1 ;;
esac

# 1. Fetch the source extract if a URL was given.
PBF_FILE="${WORK}/$(basename "${PBF_SRC%%\?*}")"
if [[ "${PBF_SRC}" == http*://* ]]; then
  [[ -f "${PBF_FILE}" ]] || wget -O "${PBF_FILE}" "${PBF_SRC}"
else
  PBF_FILE="${PBF_SRC}"
fi
OSRM_FILE="${PBF_FILE%.osm.pbf}.osrm"

run() { docker run --rm -t -v "${DATA_DIR}:/data" "${OSRM_IMAGE}" "$@"; }
rel() { echo "/data/${1#"${DATA_DIR}/"}"; }

# 2-4. extract -> partition -> customize (MLD pipeline)
run osrm-extract   -p "${LUA}" "$(rel "${PBF_FILE}")"
run osrm-partition "$(rel "${OSRM_FILE}")"
run osrm-customize "$(rel "${OSRM_FILE}")"

echo "Done. Point the ${PROFILE} backend's command at: $(rel "${OSRM_FILE}")"
