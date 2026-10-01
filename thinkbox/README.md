# thinkbox — routing box (Lenovo M72e, x86 / 2.5G NIC)

Runs both routing engines, reachable over Tailscale only (not public):
- [MOTIS](https://github.com/motis-project/motis) on `:8080`: multimodal transit
  routing over **all of the US** (GTFS timetables + OpenStreetMap street/walk routing).
- [OSRM](https://github.com/Project-OSRM/osrm-backend) on `:5000`: car / bicycle /
  foot road routing over the **US West** (Geofabrik `us-west`), stock profiles,
  Contraction Hierarchies.

Both memory-map their data, so they share the 16GB box on working set rather
than dataset size.

## Layout
- `compose.yaml` — one stack serving both (MOTIS + `osrm-nginx` + one backend per profile).
- `prep-motis.py` — builds `data/motis` (uv env from `pyproject.toml` / `uv.lock`).
- `refresh-motis.sh` + `systemd/` — rebuilds MOTIS with fresh feeds twice a month.
- `prep-osrm.py` — builds `data/osrm/<profile>` (stdlib only).
- `osrm-nginx.conf` — routes `/route/v1/<profile>/...` to the matching OSRM backend.
- `data/` — gitignored; `data/motis` and `data/osrm`.
- `example.env` — copy to `.env`; holds the Mobility Database token.

## OSRM
`prep-osrm.py` builds the car, bicycle and foot graphs with the **Contraction
Hierarchies** pipeline (`osrm-extract -> osrm-contract`). It defaults to the
Geofabrik US West extract (`north-america/us-west-latest.osm.pbf`, ~3-4GB,
downloaded into `data/osrm/`), and names the output `data/osrm/<profile>/network.osrm`
regardless of the source. We use CH rather than MLD because this backend never
applies live traffic updates, so CH's faster queries win. It reads the OSRM image
from `compose.yaml` and skips profiles already built with it, so bumping the image
there and re-running rebuilds everything.

**Build it on a bigger box, not thinkbox.** Extract and contract peak well above
16GB — on the 16GB box they thrash swap for days and can hang the machine.
Whole-US is out of reach even on the 48GB box: car extract alone needed >100GB
of RAM + swap and slowed to ~2% CPU while swapping, so we serve US West. OSRM
graphs are version-locked but machine-portable, so build on a larger host (with
the same repo/image) and copy the result over:
```bash
# on the build box (e.g. a 48GB machine), same repo + OSRM image:
# optional soft memory cap (see prep-osrm.py "Memory"):
#   sudo cp osrm-build.slice /etc/systemd/system/ && sudo systemctl daemon-reload
uv run prep-osrm.py 2>&1 | tee build.log   # --profiles car foot, --force-rebuild, --source <pbf>
# the build containers run as root and leave some files root-only (0700):
sudo chown -R "$USER": data/osrm/{car,bicycle,foot}
# then ship the graphs to thinkbox (skipping each profile's source .pbf link)
# and serve them mmap'd:
for p in car bicycle foot; do
  rsync -a --info=progress2 --exclude network.osm.pbf \
    data/osrm/$p/ thinkbox:~/clubhouse-server/thinkbox/data/osrm/$p/
done
```
On thinkbox:
```bash
docker compose up -d
curl "http://localhost:5000/route/v1/driving/-122.42,37.77;-122.41,37.78?overview=false"
```
Serving is cheap: each backend memory-maps its `.osrm.hsgr`, so serve-time RAM is
the query working set, not the dataset. `/route/v1/<profile>/...` is routed by
keyword (`driving`, `cycling`, `walking`, and aliases; see `osrm-nginx.conf`).

## MOTIS

### Routing-only profile (why it fits 16GB)
MOTIS memory-maps its dataset (`cista::mmap`), so serve-time RAM is the *working
set*, not the whole dataset. The one feature that must be fully resident — the
address/geocoding index (`adr`) — is the memory hog (~25GB on a full planet), so
`prep-motis.py` writes a `config.yml` with **`geocoding: false`,
`reverse_geocoding: false`, and tiles off**, keeping **`street_routing: true`**.
thinkbox is a pure routing backend: clients send coordinates, not place names.

The binding constraint is the **import peak** (building the US street graph,
roughly several GB). Run prep on an SSD with some swap headroom.

### Data (`data/motis`, gitignored)
Multi-GB and **not** in git (root `.gitignore` covers `**/data/`). Reproducible:

```bash
cp example.env .env             # fill in MOBILITY_DB_REFRESH_TOKEN
uv run prep-motis.py            # download US GTFS + OSM, sanitize, import
```
The prep tool's deps are managed by [uv](https://docs.astral.sh/uv/)
(`pyproject.toml` + `uv.lock`); `uv run` creates the project venv on first use,
so nothing lands in base Python. Useful flags: `--download-only`,
`--prepare-only` (sanitize + write `config.yml` but skip the slow `motis
import`, so you can inspect the staged feeds first), `--num-days N` (timetable
window, default 30), `--date YYYY-MM-DD` (reference week), `--force-download`,
`--force-rebuild`. A fast pre-import scan verifies every staged feed has a valid
`agency_timezone` before the import starts.

`prep-motis.py` discovers every US GTFS feed from the
[Mobility Database](https://mobilitydatabase.org) (free token), downloads the
Geofabrik `us-latest.osm.pbf`, sanitizes feeds (flatten nested zips, normalize
CSV whitespace/BOM/CRLF, drop feeds missing required tables), shifts expired
feeds onto the timetable window, writes `config.yml`, and runs `motis import`.
Re-runs are incremental (cached downloads; import skipped unless
`--force-rebuild`).

**Whitespace normalization matters:** some agencies (e.g. Metra) emit `", "`
delimiters, leaving a leading space that turns `America/Chicago` into
`" America/Chicago"` and fails MOTIS's strict timezone lookup — the sanitizer
trims every cell to prevent this.

**Timezone handling:** MOTIS validates *both* GTFS timezone fields —
`agency.txt:agency_timezone` (required) and `stops.txt:stop_timezone`
(optional) — and aborts the whole import on the first value it can't find in
its tz database. Prep handles both: it fills a missing/invalid `agency_timezone`
by inferring the zone from a representative stop coordinate (via
`timezonefinder`, correct for Arizona/Indiana edge cases), and blanks any
invalid `stop_timezone` (an empty one simply inherits the agency zone). A fast
pre-import scan then DROPS any straggler that still carries an unresolvable
timezone, so one bad feed can never abort the ~30-min import. `timezonefinder`
comes from the uv-managed env.

If you have no token, drop GTFS `.zip` files into `data/motis/gtfs/` manually and
prep will use those.

### Keeping timetables fresh
The import covers a fixed window (`--num-days`, 30 by default), so MOTIS stops
answering once it ends ("query time ... is outside of loaded timetable window").
`refresh-motis.sh` rebuilds it with freshly downloaded feeds:

1. Prepares `data/motis-next` from scratch while the current dataset keeps serving.
   Every GTFS feed is downloaded new, because re-running prep in place reuses
   cached zips: an unversioned feed would never update, and a versioned one would
   be imported alongside its old copy. The OSM extract is reused unless it's over
   90 days old.
2. Stops MOTIS for `motis import` (~30+ min); the server and the import don't both
   fit in 16GB.
3. Swaps folders (`data/motis` becomes `data/motis-prev`), starts MOTIS and checks
   that it plans a trip for tomorrow.

If anything fails once MOTIS is stopped, the previous dataset goes back into
service and the failed build stays in `data/motis-next`. The script exits non-zero,
so the systemd unit shows as failed.

`systemd/` runs it on the 1st and 15th at ~03:00:
```bash
sudo cp systemd/motis-refresh.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now motis-refresh.timer
systemctl list-timers motis-refresh      # next run
sudo systemctl start motis-refresh       # run now
journalctl -u motis-refresh              # logs
```

## Deploy
```bash
docker compose up -d
curl "http://localhost:8080/"          # MOTIS health / UI
curl "http://localhost:5000/route/v1/driving/-122.42,37.77;-122.41,37.78?overview=false"
```
Reachable from other mesh boxes over Tailscale at `thinkbox:8080` (MOTIS) and
`thinkbox:5000` (OSRM). Neither is exposed publicly.

### RAM fallbacks
If the full-US street import OOMs in practice:
- lower `--num-days`, or
- run transit-only: set `street_routing: false` in `config.yml` and skip the OSM
  download — MOTIS then approximates walking as straight-line footpaths (crude
  walk/transfer times, minimal RAM).
