# thinkbox — reserved worker (Lenovo M72e, x86 / 2.5G NIC)

Currently **idle by design**. Reserved for two possible future workloads:

1. **MOTIS** (transit routing) — the planned next addition. MOTIS ingests GTFS +
   OSM and is **RAM-hungry**; the M72e tops out around 16GB, so size the dataset
   accordingly. Add a `compose.yaml` here when built, and give it a backend in
   the gateway tunnel/HAProxy.
2. **OSRM #2** — if the single OSRM host (orange) saturates, copy `orange/`'s
   stack here, replicate `./data` (rsync from orange; the `.osrm` files are
   architecture-independent), and front both with HAProxy on cube.

Don't try to run a full OSRM *and* MOTIS here at once without checking RAM first.
