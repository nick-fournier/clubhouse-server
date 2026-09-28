# homelab infra

One git repo for the whole homelab, **one folder per host**, plain Docker Compose
over a Tailscale mesh. No Kubernetes — every workload is pinned to a box by
hardware, so an orchestrator's scheduler buys nothing here. See
[the migration plan](#migration-status) below.

## Topology

| Folder | Host | Arch / NIC | Runs |
|--------|------|-----------|------|
| `razz/` | razz (Pi4) | arm64 / 1G | cloudflared tunnel, static sites (`home`, `fns`, `bvi`) |
| `orange/` | orange (Pi5) | arm64 / 1G | *being repurposed* — compute worker for the portfolio lab |
| `thinkbox/` | thinkbox (M72e) | amd64 / 2.5G | Routing: MOTIS (transit, `:8080`) + OSRM (car/bike/foot, `:5000`) |
| `cube/` | cube (Unraid) | amd64 / 2×2.5G | Postgres (+ Plex, dev/gaming VM, Unraid-managed) |

Cross-host traffic uses **Tailscale MagicDNS** hostnames (`razz4`, `orange`,
`cube`), not Docker service names. Public traffic enters only through the
cloudflared tunnel on razz, and **`razz/tunnel.yml` is the list of every public
hostname**. A proxied wildcard DNS record (`*` → the tunnel) sends every
subdomain to the tunnel, so adding a hostname needs no DNS change. After
editing `tunnel.yml`, run `docker compose restart tunnel`.
- `launchpad.nicholasfournier.com` → razz `home` (index page, plus redirects for
  the old Django launchpad paths in `razz/home/nginx.conf`)
- `fns.nicholasfournier.com` → razz `fns` (image from `nick-fournier/sbyc_course_app`)
- `bvi.nicholasfournier.com` → razz `bvi` (image from `nick-fournier/bvi_itinerary`)

MOTIS and OSRM run on thinkbox and are reachable over Tailscale only (see `thinkbox/README.md`).

Static sites stay on razz so they don't go down when a worker is busy or out of
memory. Each app repo's CI pushes `nichfournier/<app>:latest`; deploy with
`docker compose pull <svc> && docker compose up -d <svc>` in `razz/`.

## Per-host bring-up

Each folder is a self-contained stack. On the target host:
```bash
cp <folder>/example.env <folder>/.env   # fill in secrets
docker compose -f <folder>/compose.yaml up -d
```
Or deploy via Portainer as a **Git-backed stack** pointing at the folder
(auto-update on push). Add workers to Portainer as standard **Agent** endpoints
over Tailscale (e.g. `orange:9001`) — Edge agents aren't needed on a mesh.

The Portainer **server** runs on cube, as an Unraid-managed container (not in
this repo). Each *other* box gets the bootstrap **agent** so the cube dashboard
can see it — run once locally on that box (it's deliberately not
Portainer-managed; see the file's header):
```bash
docker compose -f agent-compose.yaml up -d   # on razz4, orange, thinkbox
```
Then in the Portainer UI on cube: **Environments → Add environment → Agent → `<host>:9001`**.

**Why the agent is its own file, not a service in the box's `compose.yaml`:** so the
app stacks can be Portainer **git-stacks** (push → auto-redeploy). If the agent
lived inside a Portainer-managed stack, every deploy would recreate Portainer's
own connection mid-run and break the deploy. Keeping it separate lets the workload
redeploy freely while the agent stays put.
3. **Retire K3s** — once Compose serves all traffic, run `k3s-uninstall.sh` /
   `k3s-agent-uninstall.sh` on the nodes. (K8s manifests already removed from git.)

Postgres stays on cube — no database migration.
