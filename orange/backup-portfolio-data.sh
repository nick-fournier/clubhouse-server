#!/usr/bin/env bash
# Back up orange's portfolio-lab data root to cube's NAS (SMB share "bulk", mounted at /mnt/bulk).
#
#   /home/nick/portfolio-data/  ->  /mnt/bulk/portfolio-data/          a plain mirror, to browse
#                               ->  /mnt/bulk/restic/portfolio-data    dated restic snapshots
#
# The mirror follows orange: files deleted there are deleted here on the next run. The restic
# snapshots keep history: 14 daily, 8 weekly and 12 monthly; each stores only what changed.
# cache/ is left out of the snapshots (it rebuilds itself). On Sundays unused data is pruned
# and the repository checked.
#
#   Restore:  RESTIC_REPOSITORY=/mnt/bulk/restic/portfolio-data RESTIC_PASSWORD=portfolio-data \
#             restic restore latest --target /some/folder [--include /home/nick/portfolio-data/x]
#
# Refuses to run if the share isn't mounted, so it never fills orange's own disk instead.
#
# Cron (nick's crontab, after the nightly jobs finish):
#   30 3 * * * /home/nick/clones/clubhouse-server/orange/backup-portfolio-data.sh >> /home/nick/backup-portfolio-data.log 2>&1
set -euo pipefail

SOURCE=/home/nick/portfolio-data/
MOUNT=/mnt/bulk
TARGET=$MOUNT/portfolio-data/
export RESTIC_REPOSITORY=$MOUNT/restic/portfolio-data
# Not a secret (stock data and paper-trading records); restic 0.16 (Ubuntu's) needs one.
export RESTIC_PASSWORD=portfolio-data

ls "$MOUNT" > /dev/null 2>&1 || true  # wake the automount
if ! mountpoint -q "$MOUNT"; then
    echo "$(date -Is) $MOUNT is not mounted; skipping backup" >&2
    exit 1
fi

mkdir -p "$TARGET"
# SMB has no Unix owners or permissions: copy contents and file times only. --inplace: rsync's
# temporary names (".<file>.XXXX") turn "_SUCCESS" into "._SUCCESS...", which the share refuses
# (macOS metadata names); --omit-dir-times: the share doesn't keep folder times.
rsync -rt --inplace --omit-dir-times --delete --modify-window=2 "$SOURCE" "$TARGET"
echo "$(date -Is) mirrored $(du -sh "$SOURCE" | cut -f1) to $TARGET"

restic cat config > /dev/null 2>&1 || restic init
restic backup --quiet --exclude "${SOURCE}cache" "$SOURCE"
restic forget --quiet --keep-daily 14 --keep-weekly 8 --keep-monthly 12
if [ "$(date +%u)" = 7 ]; then
    restic prune --quiet
    restic check
fi
echo "$(date -Is) snapshot $(restic snapshots --latest 1 --compact | awk 'NR == 3 {print $1, $2, $3}') in $RESTIC_REPOSITORY"
