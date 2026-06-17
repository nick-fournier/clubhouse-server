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
| `thinkbox/` | thinkbox (M72e) | amd64 / 2.5G | MOTIS (transit routing, all-US, `:8080`) |
| `cube/` | cube (Unraid) | amd64 / 2×2.5G | Postgres (+ Plex, dev/gaming VM, Unraid-managed) |

Cross-host traffic uses **Tailscale MagicDNS** hostnames (`razz`, `orange`,
`cube`), not Docker service names. Public traffic enters only through the
cloudflared tunnel on razz:
- `launchpad.nicholasfournier.com` → razz nginx → Django web
- `router.nicholasfournier.com` → split by URL path over Tailscale:
  `/api/...` → `thinkbox:8080` (MOTIS transit), everything else → `orange:5000` (OSRM)

Routing is **split by host, not load-balanced**: `orange` runs OSRM (car/bike/
foot road routing) and `thinkbox` runs MOTIS (transit). They share one public
hostname but neither fronts the other — the cloudflared tunnel fans out by path
(`razz/tunnel.yml`), since the MOTIS (`/api/...`) and OSRM (`/route`, `/table`,
…) namespaces are disjoint and need no rewriting.

## Per-host bring-up

Each folder is a self-contained stack. On the target host:
```bash
cp <folder>/example.env <folder>/.env   # fill in secrets
docker compose -f <folder>/compose.yaml up -d
```
Or deploy via Portainer as a **Git-backed stack** pointing at the folder
(auto-update on push). Add workers to Portainer as standard **Agent** endpoints
over Tailscale (e.g. `orange:9001`) — Edge agents aren't needed on a mesh.

The Portainer **server** runs once on razz (in `razz-gateway/`). Each *other* box
gets the bootstrap **agent** so the razz dashboard can see it — run once locally
on that box (it's deliberately not Portainer-managed; see the file's header):
```bash
docker compose -f agent-compose.yaml up -d   # on orange, optionally cube/thinkbox
```
Then in the razz UI: **Environments → Add environment → Agent → `<host>:9001`**.

**Why the agent is its own file, not a service in the box's `compose.yaml`:** so the
app stacks can be Portainer **git-stacks** (push → auto-redeploy). If the agent
lived inside a Portainer-managed stack, every deploy would recreate Portainer's
own connection mid-run and break the deploy. Keeping it separate lets the workload
redeploy freely while the agent stays put.
3. **Retire K3s** — once Compose serves all traffic, run `k3s-uninstall.sh` /
   `k3s-agent-uninstall.sh` on the nodes. (K8s manifests already removed from git.)

Postgres stays on cube — no database migration.
