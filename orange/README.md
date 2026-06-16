# orange — OSRM routing worker

Runs OSRM (`osrm-nginx` on :5000 fronting per-profile `osrm-routed` backends).
This is the **only OSRM host** for now; if it saturates, duplicate this stack
onto `thinkbox/` and add HAProxy (see the top-level README).

## Layout
- `compose.yaml` — the stack (absorbed from the old `osrm-server` repo).
- `nginx.conf` — routes `/route/v1/<profile>` by regex to each backend.
- `prep-data.sh` — regenerates the gitignored `./data` graph files.

## Data (`./data`, gitignored)
The compiled `.osrm` files are large and **not** in git. They are reproducible
with `prep-data.sh` for the standard car/bike/foot profiles. The compose expects:

| backend | data path |
|---------|-----------|
| car     | `/data/car/cropped_network.osrm` |
| bicycle | `/data/bicycle.uswest_wrongways/us-west-latest.osrm` |
| foot    | `/data/foot/cropped_network.osrm` |
| custom  | `/data/custom_network/custom_network.osrm` (OSRM v6) |

⚠️ **Gap to close:** the custom / cropped / wrong-ways variants were built with
hand-edited Lua profiles that currently exist only on orange's disk. Commit them
to `orange/profiles/` so the whole dataset is reproducible, not just the stock
profiles.

## Deploy
Via Portainer (Git-backed stack pointing at this folder) or directly:
```bash
docker compose up -d
curl "http://localhost:5000/route/v1/driving/-122.4,37.7;-122.3,37.8"
```

## Deferred
The `custom_nowrongways` profile (backend on :5005) is commented out in both
`compose.yaml` and `nginx.conf` — its upstream had no backend and blocked nginx
startup. Re-enable both together once its data exists.
