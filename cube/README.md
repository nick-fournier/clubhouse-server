# cube — Unraid server (Postgres, Plex, dev/gaming VM)

cube is an **Unraid** box. Plex, the dev/gaming VM, and day-to-day container
management are handled by Unraid itself — they are intentionally **not** captured
as compose stacks here. This folder exists for the one thing worth version
controlling: the **custom Postgres image** and its backup.

> **No database migration.** Postgres already lives here. Earlier drafts proposed
> moving it from "thinkbox" — that was based on stale naming and is not happening.

## What's here
- `Dockerfile` — `postgres:17` + a cron job that runs `backup.sh` daily.
- `backup.sh` — `pg_dump -F c` into the mounted `/backups`.
- `compose.yaml` — pulls `nichfournier/launchpad-postgres:latest` + pgAdmin.
- `compose.build.yaml` — overlay to build the image locally instead of pulling.

The image is built and pushed multi-arch by `.github/workflows/build-images.yml`
(build context is `./cube`).

## Running it
On Unraid you can either run the published image via a Docker template, or use
this compose directly:
```bash
cp example.env .env     # set POSTGRES_*, PGADMIN_*, BACKUP_PATH
docker compose up -d
# build locally instead of pulling:
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

## Reached by
The Django app (on razz) connects over Tailscale with `PG_HOST=cube`. The OSRM
batch client VM also lives on this box.

## Note
`container_name` is still `thinkbox-postgres` for continuity with the existing
volume/backups — rename only if you also migrate the data volume.
