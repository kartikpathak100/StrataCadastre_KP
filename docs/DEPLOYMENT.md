# Deploying Strata Cadastre

SIH26011 — 3D ULPIN Generation and Vertical Property Mapping System

Self-hosted, container-based deployment. No managed PaaS anywhere in the
stack — which is also the honest architecture for this problem statement,
since real DoLR/NIC land-records infrastructure runs on government data
centres and on-prem servers, not third-party platforms.

---

## Architecture

One host, three containers, one open port.

```
                        ┌─────────────────────────────────┐
   Internet ─── :80 ───►│  web    nginx                   │
                        │         · serves built viewer   │
                        │         · proxies /api ─────────┼──┐
                        └─────────────────────────────────┘  │
                                                             │
                        ┌─────────────────────────────────┐  │
                        │  api    FastAPI + uvicorn       │◄─┘
                        │         · 3D-ULPIN generator    │
                        │         · fusion pipeline       │
                        └────────────────┬────────────────┘
                                         │
                        ┌────────────────▼────────────────┐
                        │  db     PostgreSQL 17           │
                        │         + PostGIS 3.5 + SFCGAL  │
                        └─────────────────────────────────┘
                              (no host port — internal only)
```

The frontend and API are served from the **same origin**, so the browser
never makes a cross-origin request and **no CORS configuration is needed**.
That's the main practical reason nginx sits in front rather than exposing
the API on its own port.

Files involved:

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | The whole stack |
| `Dockerfile.api` | Backend image |
| `Dockerfile.web` | Frontend build → nginx image |
| `nginx.conf` | Static serving + `/api` reverse proxy |
| `requirements.txt` | Python dependencies |
| `.env.example` | Configuration template |
| `schema_3d_cadastre.sql` + `schema_patch_01.sql` | Database schema |
| `verify_schema.sql` | Nine-check database smoke test |

---

## Part A — Repository layout

Arrange the repo like this before building anything:

```
strata-cadastre/
├── docker-compose.prod.yml
├── docker-compose.yml            # db only, for local schema work
├── Dockerfile.api
├── Dockerfile.web
├── nginx.conf
├── requirements.txt
├── .env.example
├── .dockerignore
├── main.py
├── pipeline.py
├── geodesy.py
├── schema_3d_cadastre.sql
├── schema_patch_01.sql
├── verify_schema.sql
├── index.html                    # standalone fallback demo
└── strata-ui/                    # Vite app (created in Part B)
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── App.jsx
        └── CadastreViewer3D.jsx
```

---

## Part B — Create the frontend app

`CadastreViewer3D.jsx` is a component, not a whole app. Scaffold the app
around it once:

```bash
npm create vite@latest strata-ui -- --template react
cd strata-ui
npm install cesium vite-plugin-cesium
```

Replace `vite.config.js` with:

```js
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import cesium from 'vite-plugin-cesium';

export default defineConfig({
  plugins: [react(), cesium()],
  server: { host: true, port: 5173 },
});
```

**`vite-plugin-cesium` is not optional.** Without it, Cesium's workers and
widget CSS are never served and you get a blank globe with console 404s —
the single most common CesiumJS setup failure.

Copy `CadastreViewer3D.jsx` into `strata-ui/src/`, then make `src/App.jsx`:

```jsx
import CadastreViewer3D from './CadastreViewer3D';

export default function App() {
  return <CadastreViewer3D />;
}
```

Check it runs before containerising anything:

```bash
npm run dev      # http://localhost:5173
```

---

## Part C — Run the whole stack locally

```bash
cd ..                       # back to repo root
cp .env.example .env
```

Edit `.env` and set a real password:

```bash
openssl rand -base64 24     # paste into POSTGRES_PASSWORD
```

Then:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

First build takes several minutes — Cesium is a large dependency. When it
finishes, open `http://localhost`.

```bash
docker compose -f docker-compose.prod.yml ps        # all three healthy?
docker compose -f docker-compose.prod.yml logs -f   # watch startup
```

Verify the database actually works:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  psql -U strata -d strata -v ON_ERROR_STOP=1 < verify_schema.sql
```

Nine checks must print PASS. If `CHECK 1` fails on SFCGAL, you're on the
wrong image (see Part E). If `CHECK 3` reports a volume off by roughly
1e10, `schema_patch_01.sql` didn't apply — volumes are being measured in
degrees rather than metres.

**If it doesn't work locally, it will not work on a server.** Don't move on
until this is green.

---

## Part D — Provision a server

Any x86-64 Linux box with Docker. Options, roughly cheapest first:

| Option | Notes |
|---|---|
| **College / lab server** | Free, and "deployed on institute infrastructure" sounds good in a demo |
| **Hetzner Cloud** | ~€4/mo, 2 vCPU / 4 GB — plenty for this |
| **DigitalOcean Droplet** | ~$6/mo, lots of tutorials if you get stuck |
| **AWS EC2 free tier** | t2.micro / t3.micro; 1 GB RAM is tight but workable |
| **Your own laptop** | Genuinely fine for a hackathon demo — see Part H |

**Pick an x86-64 (amd64) instance.** The official `postgis/postgis` images
are published for amd64 only, so ARM instances — Oracle Cloud's free Ampere
tier, Hetzner's ARM plans, Apple Silicon without emulation — will fail to
start the database. This catches people out precisely because the free ARM
tiers look like the most attractive option.

Minimum practical spec: **2 GB RAM**. The Cesium build is memory-hungry; on
a 1 GB box, build the web image locally and push it rather than building on
the server.

### Install Docker (Ubuntu 22.04 / 24.04)

```bash
ssh root@YOUR_SERVER_IP

apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

### Create a non-root user

```bash
adduser strata
usermod -aG docker strata
rsync --archive --chown=strata:strata ~/.ssh /home/strata/
```

Reconnect as that user from here on: `ssh strata@YOUR_SERVER_IP`

### Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp      # only if you'll add TLS in Part F
sudo ufw enable
sudo ufw status
```

Port 5432 stays closed. `docker-compose.prod.yml` deliberately gives the
database no host port, so it's reachable only from the `api` container. An
internet-exposed Postgres with a weak password gets found by automated
scanners within hours — this is not a theoretical risk.

---

## Part E — Deploy

```bash
# On the server
git clone YOUR_REPO_URL strata-cadastre
cd strata-cadastre

cp .env.example .env
nano .env                   # set POSTGRES_PASSWORD to a strong value
```

No git remote? Copy from your machine instead:

```bash
rsync -av --exclude node_modules --exclude .venv --exclude .env \
  ./strata-cadastre/ strata@YOUR_SERVER_IP:~/strata-cadastre/
```

Build and start:

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Verify the database on the server too:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  psql -U strata -d strata -v ON_ERROR_STOP=1 < verify_schema.sql
```

Visit `http://YOUR_SERVER_IP`.

### If SFCGAL fails

`postgis_sfcgal` is what provides `ST_Volume`, `ST_MakeSolid`,
`ST_3DIntersection` and `ST_3DArea` — the functions volumetric cadastre
depends on. Plain PostGIS cannot do 3D volume work at all.

Two traps, both of which produce a database that looks fine until a 3D
function is called:

1. **Alpine variants of `postgis/postgis` don't support SFCGAL.** Use a
   Debian tag — the compose file pins `postgis/postgis:17-3.5`.
2. **Even on Debian it ships available-but-not-enabled.** The
   `CREATE EXTENSION postgis_sfcgal;` line in `schema_3d_cadastre.sql` is
   mandatory, not decorative.

Confirm directly:

```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U strata -d strata -c "SELECT postgis_sfcgal_version();"
```

Also relevant if anyone suggests moving to managed hosting later: **AWS RDS
does not offer SFCGAL** (`extension "postgis_sfcgal" is not available`).
Among managed providers, Neon documents support for it; most others don't.

---

## Part F — Domain and HTTPS (optional)

Skip this if you're demoing on an IP address. If you have a domain, point
an A record at your server, then:

```bash
sudo apt install -y certbot
docker compose -f docker-compose.prod.yml stop web
sudo certbot certonly --standalone -d cadastre.yourdomain.in
```

Mount the certificates by adding this to the `web` service in
`docker-compose.prod.yml`:

```yaml
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt:ro
```

And add a TLS server block to `nginx.conf`:

```nginx
server {
    listen 443 ssl;
    server_name cadastre.yourdomain.in;

    ssl_certificate     /etc/letsencrypt/live/cadastre.yourdomain.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cadastre.yourdomain.in/privkey.pem;

    # ... copy the root/ and location blocks from the port-80 server ...
}
```

Rebuild: `docker compose -f docker-compose.prod.yml up -d --build web`

---

## Part G — Operations

```bash
# Logs
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f web

# Restart one service
docker compose -f docker-compose.prod.yml restart api

# Deploy an update
git pull
docker compose -f docker-compose.prod.yml up -d --build

# Database shell
docker compose -f docker-compose.prod.yml exec db psql -U strata -d strata

# Backup
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U strata strata | gzip > backup_$(date +%F).sql.gz

# Restore
gunzip -c backup_2026-09-09.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db psql -U strata -d strata

# Full reset — DESTROYS DATA. Needed after editing the schema, because the
# init scripts only run on an empty volume.
docker compose -f docker-compose.prod.yml down -v
docker compose -f docker-compose.prod.yml up -d --build
```

---

## Part H — Demo-day fallback plan

Judges don't care why something is broken. Have layers, and rehearse the
bottom ones:

1. **Deployed server** — `http://YOUR_SERVER_IP`
2. **Everything on your laptop** — `docker compose -f docker-compose.prod.yml up -d`.
   Immune to venue wifi entirely.
3. **No containers** — `python main.py` (SQLite, no database needed) plus
   `npm run dev`.
4. **No server at all** — open `index.html` in a browser. Self-contained,
   one CDN script, works offline once cached.
5. **Screen recording** — a clean two-minute run captured while everything
   worked. This has never once failed to connect to venue wifi.

Layers 4 and 5 cost almost nothing and are the difference between a bad
five minutes and no demo.

---

## Part I — Pre-demo checklist

- [ ] `verify_schema.sql` prints nine PASSes on the machine you'll demo from
- [ ] `python main.py` self-tests pass
- [ ] Viewer loads with the ion token **and** with it blank
- [ ] All three containers show `healthy` in `docker compose ps`
- [ ] Deviation mode rehearsed live (green sanctioned vs red unauthorised)
- [ ] Citizen ⇄ Registrar toggle rehearsed (the DPDP redaction moment)
- [ ] `index.html` opens with wifi switched off
- [ ] Backup video recorded
- [ ] Laptop tested on venue wifi, or a phone hotspot ready
- [ ] `.env` is in `.gitignore` and no password is in the repo

---

## Part J — Known gaps, in your own words

State these before a judge finds them. Being straight about limitations
reads as engineering maturity; being caught reads as the opposite.

- **`geodesy.py` uses a coarse geoid lookup table**, not full EGM2008
  synthesis. It follows the documented shape of the field over India at
  roughly ±1–3 m. Production swaps in a real geoid grid — one function, and
  the code says which.
- **`pipeline.py` runs on numpy/scipy, not PDAL/Shapely/Trimesh/GeoPandas.**
  Every substitution names its production equivalent. The maths was
  validated against analytic ground truth: the deviation detector lands
  within **1.56%** of the true unauthorised volume.
- **Ingestion is synthetic.** Real drone/LiDAR/CAD parsing is an explicit
  `NotImplementedError` branch, not a silent fake.
- **The API ships on SQLite.** The PostGIS schema is real and tested by
  `verify_schema.sql`, and `DATABASE_URL` is already plumbed to the `api`
  container — the swap is two functions in `main.py`.
- **The viewer renders Cesium entities, not 3D Tiles.** Correct at single-
  parcel scale; 3D Tiles is the next step for true city scale.
- **Phase 1 shipped with a units bug.** Volumes were computed on EPSG:4326
  degrees instead of projected metres — wrong by ~1e10, and it never
  errored. Writing `verify_schema.sql` found it; `schema_patch_01.sql` fixed
  it; `CHECK 3` now guards it permanently.

That last one is worth telling if anyone asks how you tested this. "We wrote
a verification suite and it caught a ten-orders-of-magnitude error in our
own schema" is a far stronger answer than "it all worked first time."

---

## Appendix — Troubleshooting

| Symptom | Cause |
|---|---|
| Blank globe, console 404s on Cesium assets | `vite-plugin-cesium` missing from `vite.config.js` |
| `extension "postgis_sfcgal" is not available` | Alpine image, or ARM host, or RDS |
| `CHECK 3` volume off by ~1e10 | `schema_patch_01.sql` not applied |
| `db` won't start on a new host | ARM CPU — `postgis/postgis` is amd64-only |
| Schema edits don't take effect | Init scripts only run on an empty volume; `down -v` first |
| Web build OOM-killed | Under 2 GB RAM; build the image locally and push it |
| `/api` returns 502 | `api` container unhealthy — check its logs |
| Frontend loads but API calls fail | Rebuild `web` after changing `VITE_API_URL`; Vite inlines it at build time |
| `POSTGRES_PASSWORD` variable not set | `.env` missing or not in the same directory as the compose file |
