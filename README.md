# homelab infra

One git repo for the whole homelab, **one folder per host**, plain Docker Compose
over a Tailscale mesh. No Kubernetes — every workload is pinned to a box by
hardware, so an orchestrator's scheduler buys nothing here. See
[the migration plan](#migration-status) below.

## Topology

| Folder | Host | Arch / NIC | Runs |
|--------|------|-----------|------|
| `razz-gateway/` | razz (Pi4) | arm64 / 1G | Portainer server, cloudflared tunnel, Django `web` + nginx |
| `orange/` | orange (Pi5) | arm64 / 1G | OSRM (`osrm-nginx` :5000 + per-profile backends) |
| `thinkbox/` | thinkbox (M72e) | amd64 / 2.5G | *idle* — reserved for MOTIS (future) |
| `cube/` | cube (Unraid) | amd64 / 2×2.5G | Postgres (+ Plex, dev/gaming VM, Unraid-managed) |

Cross-host traffic uses **Tailscale MagicDNS** hostnames (`razz`, `orange`,
`cube`), not Docker service names. Public traffic enters only through the
cloudflared tunnel on razz:
- `launchpad.nicholasfournier.com` → razz nginx → Django web
- `router.nicholasfournier.com` → `orange:5000` (OSRM) over Tailscale

There is **no load balancer**: OSRM runs on a single host for now. If orange
saturates, duplicate `orange/` onto `thinkbox/` and add HAProxy on cube.

## Per-host bring-up

Each folder is a self-contained stack. On the target host:
```bash
cp <folder>/example.env <folder>/.env   # fill in secrets
docker compose -f <folder>/compose.yaml up -d
```
Or deploy via Portainer as a **Git-backed stack** pointing at the folder
(auto-update on push). Add workers to Portainer as standard **Agent** endpoints
over Tailscale (e.g. `orange:9001`) — Edge agents aren't needed on a mesh.

## Migration status

Migrating off a 4-node K3s cluster. Three independent, individually-safe steps:

1. **OSRM into git + dashboard** — `orange/` now holds the OSRM stack (absorbed
   from the old `osrm-server` repo) with reproducible data (`orange/prep-data.sh`).
   Stand up Portainer on razz, add orange as an agent. ✅ repo work done.
2. **Control plane to the Pi** — `razz-gateway/` runs the tunnel + web + dashboard;
   `tunnel.yml` repointed at `orange:5000`. Deploy on razz.
3. **Retire K3s** — once Compose serves all traffic, run `k3s-uninstall.sh` /
   `k3s-agent-uninstall.sh` on the nodes. (K8s manifests already removed from git.)

Postgres stays on cube — no database migration.
