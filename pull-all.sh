#!/usr/bin/env bash
# pull-all.sh — fast-forward the clubhouse-server checkout on every host over SSH.
# --ff-only refuses to merge, so a host with local changes or diverged history
# errors out instead. Pulling doesn't redeploy: run `docker compose up -d` where needed.
#
# Usage: ./pull-all.sh [host...]   (default: all hosts)

for h in ${@:-razz4 orange thinkbox cubevm}; do
  echo "== $h"
  ssh -o ConnectTimeout=5 "$h" 'cd ~/clones/clubhouse-server 2>/dev/null || cd ~/clubhouse-server && pwd && git pull --ff-only'
done
