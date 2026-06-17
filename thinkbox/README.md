# thinkbox — MOTIS transit routing (Lenovo M72e, x86 / 2.5G NIC)

Runs [MOTIS](https://github.com/motis-project/motis) — multimodal transit
routing over **all of the US** (GTFS timetables + OpenStreetMap street/walk
routing) on `:8080`. This box owns MOTIS; `orange` owns OSRM — the two routing
responsibilities are split across hosts, so there's no HAProxy here.

## Layout
- `compose.yaml` — the MOTIS server stack (serves the prebuilt `./data`).
- `prep-data.py` — builds the gitignored `./data` dataset (the OSRM `prep-data.sh` analog).
- `example.env` — copy to `.env`; holds the Mobility Database token.

## Routing-only profile (why it fits 16GB)
MOTIS memory-maps its dataset (`cista::mmap`), so serve-time RAM is the *working
set*, not the whole dataset. The one feature that must be fully resident — the
address/geocoding index (`adr`) — is the memory hog (~25GB on a full planet), so
`prep-data.py` writes a `config.yml` with **`geocoding: false`,
`reverse_geocoding: false`, and tiles off**, keeping **`street_routing: true`**.
thinkbox is a pure routing backend: clients send coordinates, not place names.

The binding constraint is the **import peak** (building the US street graph,
roughly several GB). Run prep on an SSD with some swap headroom.

## Data (`./data`, gitignored)
Multi-GB and **not** in git (root `.gitignore` covers `**/data/`). Reproducible:

```bash
cp example.env .env             # fill in MOBILITY_DB_REFRESH_TOKEN
uv run prep-data.py             # download US GTFS + OSM, sanitize, import
```
The prep tool's deps are managed by [uv](https://docs.astral.sh/uv/)
(`pyproject.toml` + `uv.lock`); `uv run` creates the project venv on first use,
so nothing lands in base Python. Useful flags: `--download-only`,
`--prepare-only` (sanitize + write `config.yml` but skip the slow `motis
import`, so you can inspect the staged feeds first), `--num-days N` (timetable
window, default 30), `--date YYYY-MM-DD` (reference week), `--force-download`,
`--force-rebuild`. A fast pre-import scan verifies every staged feed has a valid
`agency_timezone` before the import starts.

`prep-data.py` discovers every US GTFS feed from the
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

**Timezone inference:** MOTIS requires `agency_timezone`, but some feeds omit it.
Prep infers the zone from a representative stop coordinate (via `timezonefinder`,
correct for Arizona/Indiana edge cases) and injects it, rather than dropping the
feed. `timezonefinder` comes from the uv-managed env.

If you have no token, drop GTFS `.zip` files into `./data/gtfs/` manually and
prep will use those.

## Deploy
Via Portainer (Git-backed stack pointing at this folder) or directly:
```bash
docker compose up -d
curl "http://localhost:8080/"          # health / UI
```
Reachable from other mesh boxes over Tailscale at `thinkbox:8080`, and publicly
at `https://router.nicholasfournier.com/api/...` — the cloudflared tunnel on
razz routes that hostname's `/api/...` paths here and everything else to OSRM on
orange (see `razz/tunnel.yml` and the top-level README). MOTIS's whole API lives
under `/api/`, so the split needs no path rewriting. The MOTIS web UI at `/` is
*not* exposed publicly (that path goes to OSRM); reach it over Tailscale if
needed.

## RAM fallbacks
If the full-US street import OOMs in practice:
- lower `--num-days`, or
- run transit-only: set `street_routing: false` in `config.yml` and skip the OSM
  download — MOTIS then approximates walking as straight-line footpaths (crude
  walk/transfer times, minimal RAM).
