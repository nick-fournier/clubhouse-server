# thinkbox — routing box (Lenovo M72e, x86 / 2.5G NIC)

Runs both routing engines, reachable over Tailscale only (not public):
- [MOTIS](https://github.com/motis-project/motis) on `:8080`: multimodal transit
  routing over **all of the US** (GTFS timetables + OpenStreetMap street/walk routing).
- [OSRM](https://github.com/Project-OSRM/osrm-backend) on `:5000`: car / bicycle /
  foot road routing over **all of the US**, stock profiles, Contraction Hierarchies.

Both memory-map their data, so they share the 16GB box on working set rather
than dataset size.

## Layout
- `compose.yaml` — one stack serving both (MOTIS + `osrm-nginx` + one backend per profile).
- `prep-motis.py` — builds `data/motis` (uv env from `pyproject.toml` / `uv.lock`).
- `prep-osrm.py` — builds `data/osrm/<profile>` (stdlib only).
- `osrm-nginx.conf` — routes `/route/v1/<profile>/...` to the matching OSRM backend.
- `data/` — gitignored; `data/motis` and `data/osrm`.
- `example.env` — copy to `.env`; holds the Mobility Database token.

## OSRM
`prep-osrm.py` builds the car, bicycle and foot graphs with the **Contraction
Hierarchies** pipeline (`osrm-extract -> osrm-contract`). It defaults to the
whole-US Geofabrik extract (`north-america/us-latest.osm.pbf`, ~11GB, downloaded
into `data/osrm/`), and names the output `data/osrm/<profile>/network.osrm`
regardless of the source. We use CH rather than MLD because this backend never
applies live traffic updates, so CH's faster queries win. It reads the OSRM image
from `compose.yaml` and skips profiles already built with it, so bumping the image
there and re-running rebuilds everything.

**Build it on a bigger box, not thinkbox.** At whole-US scale `osrm-contract`
peaks well above 16GB — on the 16GB box it thrashes swap for days and can hang the
machine. OSRM graphs are version-locked but machine-portable, so build on a
larger host (with the same repo/image) and copy the result over:
```bash
# on the build box (e.g. a 48GB machine), same repo + OSRM image:
python3 prep-osrm.py          # --profiles car foot, --force-rebuild, --source <pbf>
# then ship the graphs to thinkbox and serve them mmap'd:
rsync -a data/osrm/ thinkbox:~/clubhouse-server/thinkbox/data/osrm/
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
