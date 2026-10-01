#!/usr/bin/env bash
# refresh-motis.sh — rebuild the MOTIS dataset with fresh GTFS feeds and swap it in.
#
# Run by motis-refresh.timer (see systemd/), or by hand in tmux. Steps:
#   1. Prepare data/motis-next from scratch while the current dataset keeps
#      serving. A fresh folder means every GTFS feed is downloaded new (the
#      download cache would otherwise keep stale and superseded feeds), and a
#      failed feed discovery is fatal instead of quietly reusing old zips. The
#      OSM extract is hard-linked over unless it's older than OSM_MAX_AGE_DAYS.
#   2. Stop MOTIS and run `motis import` (the server and the import don't both
#      fit in 16GB).
#   3. Swap folders (the current dataset becomes data/motis-prev, kept for a
#      manual rollback until the next run), start MOTIS, and check that it can
#      plan a trip for tomorrow.
# If anything fails after MOTIS stops, the previous dataset is put back and
# MOTIS restarted on it; a failed new dataset is left in data/motis-next.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"

DATA=$PWD/data
OSM=us-latest.osm.pbf
OSM_MAX_AGE_DAYS=90

log() { echo "$(date '+%F %T') $*"; }

# `motis import` writes root-owned files, so deleting a dataset needs root.
rm_dataset() { docker run --rm -v "$DATA:/d" alpine:3 rm -rf "/d/$1"; }

# SF -> Oakland tomorrow: fails on an expired timetable or a broken import.
healthy() {
  local t
  t=$(date -u -d 'tomorrow 20:00' +%Y-%m-%dT%H:%M:%SZ)
  curl -sf -m 60 "http://localhost:8080/api/v1/plan?fromPlace=37.7749,-122.4194&toPlace=37.8044,-122.2712&time=$t" |
    python3 -c 'import sys, json; sys.exit(0 if json.load(sys.stdin).get("itineraries") else 1)' 2>/dev/null
}

wait_healthy() {
  for _ in $(seq 60); do
    healthy && return 0
    sleep 10
  done
  return 1
}

swapped=0
rollback() {
  trap - ERR
  log "FAILED: restarting MOTIS on the previous dataset"
  docker compose stop motis || true
  [ "$swapped" = 1 ] && mv "$DATA/motis" "$DATA/motis-next"
  [ -d "$DATA/motis" ] || mv "$DATA/motis-prev" "$DATA/motis"
  docker compose up -d --force-recreate motis
  exit 1
}

log "preparing data/motis-next (MOTIS keeps serving)"
rm_dataset motis-next
mkdir -p "$DATA/motis-next"
if [ -n "$(find "$DATA/motis/$OSM" -mtime -"$OSM_MAX_AGE_DAYS" 2>/dev/null)" ]; then
  ln "$DATA/motis/$OSM" "$DATA/motis-next/$OSM"
else
  log "OSM extract missing or older than $OSM_MAX_AGE_DAYS days; prep will download it"
fi
MOTIS_DATA_DIR=$DATA/motis-next uv run prep-motis.py --prepare-only

log "stopping MOTIS for the import"
docker compose stop motis
trap rollback ERR
MOTIS_DATA_DIR=$DATA/motis-next uv run prep-motis.py --import-only

log "swapping in the new dataset"
rm_dataset motis-prev
mv "$DATA/motis" "$DATA/motis-prev"
mv "$DATA/motis-next" "$DATA/motis"
swapped=1
docker compose up -d --force-recreate motis

log "waiting for MOTIS to plan tomorrow's trips"
wait_healthy || rollback
trap - ERR
log "done; previous dataset kept in data/motis-prev until the next run"
