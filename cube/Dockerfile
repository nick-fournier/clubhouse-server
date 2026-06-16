# Use the official PostgreSQL 17 Bookworm image
FROM postgres:17-bookworm

# Install cron
RUN apt-get update && apt-get install -y cron

# Copy the backup script into the container
COPY backup.sh /usr/local/bin/backup.sh

# Give execute permissions to the script
RUN chmod +x /usr/local/bin/backup.sh

# Create a crontab file to schedule the backup job (daily at 2 AM Local Time UTC)
RUN echo "0 11 * * * root /usr/local/bin/backup.sh >> /var/log/cron.log 2>&1" > /etc/cron.d/pg_backup

# Give cron the appropriate permissions
RUN chmod 0644 /etc/cron.d/pg_backup && chown root:root /etc/cron.d/pg_backup

# Start cron and PostgreSQL in the foreground
CMD cron && tail -f /var/log/cron.log & docker-entrypoint.sh postgres
