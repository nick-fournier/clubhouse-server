#!/usr/bin/env bash
# Mirror orange's portfolio-lab data root to cube's NAS (SMB share "bulk", mounted at /mnt/bulk).
#
#   /home/nick/portfolio-data/{free,sharadar,trading}  ->  /mnt/bulk/portfolio-data/
#
# A mirror, not a versioned backup: files deleted on orange are deleted here on the next run
# (tearing down sharadar/ removes it from the NAS too). For history, snapshot the share on cube.
# Refuses to run if the share isn't mounted, so it never fills orange's own disk instead.
#
# Cron (nick's crontab, after the nightly jobs finish):
#   30 3 * * * /home/nick/clones/clubhouse-server/orange/backup-portfolio-data.sh >> /home/nick/backup-portfolio-data.log 2>&1
set -euo pipefail

SOURCE=/home/nick/portfolio-data/
MOUNT=/mnt/bulk
TARGET=$MOUNT/portfolio-data/

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
echo "$(date -Is) backed up $(du -sh "$SOURCE" | cut -f1) to $TARGET"
