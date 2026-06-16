#!/bin/bash

# Load environment variables
. /etc/environment

# Ensure variables are explicitly exported
export POSTGRES_USER POSTGRES_DB

# Define the backup file path and database name
BACKUP_FILE="/backups/backup_$(date +\%Y\%m\%d-%H\%M\%S).sql"

echo "exporting database to $BACKUP_FILE with command:"
echo "/usr/bin/pg_dump -U $POSTGRES_USER -F c -b -v -f $BACKUP_FILE $POSTGRES_DB"

# Number of backups to retain (older ones are pruned after a successful dump)
KEEP=5

# Run pg_dump to create the backup
if /usr/bin/pg_dump -U $POSTGRES_USER -F c -b -v -f $BACKUP_FILE $POSTGRES_DB; then
    # Rotate: keep only the $KEEP most recent backups, delete the rest.
    # Only runs on a successful dump, so a failed dump never deletes good backups.
    ls -1t /backups/backup_*.sql 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
    echo "Backup complete. Retained the $KEEP most recent backups."
else
    echo "pg_dump failed; leaving existing backups untouched." >&2
    rm -f "$BACKUP_FILE"   # drop the partial/empty file from this failed run
    exit 1
fi
