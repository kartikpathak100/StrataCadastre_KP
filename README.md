# Strata Cadastre

**3D ULPIN Generation and Vertical Property Mapping System**
Smart India Hackathon 2026 · Problem Statement **26011**
Ministry of Rural Development — Department of Land Resources (DoLR)

> Indian land records identify **parcels of ground**. A twelve-storey building
> is one parcel — twenty-four flats share a single ULPIN, and basements,
> parking, air rights and utility corridors have no unique identity at all.
> Strata Cadastre gives every one of those volumes its own legally distinct
> 3D identifier.

---

## The problem in one picture

A metro viaduct crosses the airspace above a plot. A municipal water main runs
beneath it. The family in between owns the land. **All three are true at once —
and a 2D land record has no field for any of it.** It is a polygon on a plane.

This is not an inconvenience. It is a structural limitation of the
representation.

---

## What this repository contains

| Component | File | What it does |
|---|---|---|
| Geodesy | `geodesy.py` | GNSS ellipsoidal → orthometric height (`H = h − N`) |
| Fusion pipeline | `pipeline.py` | LiDAR/CAD fusion, ICP alignment, topology validation, deviation detection |
| ML classifier | `ml_classifier.py` | Random Forest — separates building / ground / vegetation points |
| Floor plans | `floorplan_extract.py` | Boundary + room segmentation from plan images (OpenCV watershed) |
| Real-data ingest | `ingest_real.py` | LAS/LAZ/PLY/XYZ readers, incl. a dependency-free LAS implementation |
| GIS I/O | `geo_io.py` | GeoJSON parcels, DEM/DSM rasters, 3D GeoJSON export |
| API + ULPIN engine | `main.py` | FastAPI endpoints and the 28-character 3D-ULPIN generator |
| Database | `db/` | PostGIS 3D schema, patch, and a 9-check verification suite |
| Viewers | `web/` | Standalone 3D demo + React/CesiumJS component |

---

## Quick start — 30 seconds

```bash
open web/demo.html        # macOS
xdg-open web/demo.html    # Linux
```

No install, no server, no build step. Click any volume to open its 3D-ULPIN
record, toggle deviation mode, or switch between Citizen and Registrar access
tiers.

*(Open it once with an internet connection so Three.js caches from the CDN;
after that it works offline.)*

---

## Run the backend

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

`main.py` runs its self-tests first, then serves the API on `:8000`
(`/docs` for interactive documentation).

### Run each module's own test suite

Every module is executable and validates itself against known ground truth:

```bash
python geodesy.py             # height conversion + round-trip check
python pipeline.py            # fusion engine, ICP, deviation detection
python ml_classifier.py       # trains the classifier, prints held-out metrics
python floorplan_extract.py   # extracts a plan, checks area against truth
python ingest_real.py         # LAS/PLY round-trip, scale correction
python geo_io.py              # GeoJSON + DEM round-trip
python main.py                # end-to-end: ingest → ULPIN → topology → tax
```

---

## Database

```bash
docker compose up -d
docker compose exec -T db psql -U strata -d strata -v ON_ERROR_STOP=1 < db/verify_schema.sql
```

Nine checks must print `PASS`.

**Requires `postgis_sfcgal`**, not just PostGIS — it provides `ST_Volume`,
`ST_MakeSolid` and `ST_3DIntersection`. Two traps worth knowing: **Alpine
PostGIS images do not support SFCGAL**, and **AWS RDS does not offer it at
all**. Use a Debian `postgis/postgis` tag (pinned in the compose file), Neon,
or self-hosted Postgres.

---

## The 3D-ULPIN

28 characters, five segments, built **on top of** the existing government
14-digit ULPIN — this extends the statutory identifier, it does not replace it.

```
240124  11000088421000  T  906  T010
  │           │         │   │     │
  │           │         │   │     └── unit ID          (4)
  │           │         │   └──────── floor index      (3)
  │           │         └──────────── vertical type    (1)
  │           └────────────────────── existing 2D ULPIN (14)
  └────────────────────────────────── LGD locator      (6)
```

**Vertical types:** `B` basement · `G` ground · `F` floor · `T` terrace /
air-rights · `U` subsurface utility · `E` elevated corridor

---

## Measured results

All figures below are produced by the test suites in this repository, not
estimates. Run them yourself.

| Metric | Result | Source |
|---|---|---|
| ML classifier accuracy (held-out) | **99.39%** | `ml_classifier.py` |
| Building extraction precision / recall | **98.66% / 99.58%** | `ml_classifier.py` |
| Deviation detection vs analytic truth | **1.56%** | `pipeline.py` |
| Unauthorised volume detected | **528 m³** | `pipeline.py` |
| ICP alignment (12° known transform) | **0.022° / 1 mm** | `pipeline.py` |
| Plinth / apex extraction from LiDAR | **±1 cm / ±7 cm** | `pipeline.py` |
| Floor plan boundary area error | **1.32%** | `floorplan_extract.py` |
| Database integrity checks | **9 / 9 pass** | `db/verify_schema.sql` |

---

## Honest limitations

Stated up front, because a system that overclaims is worth less than one whose
boundaries are known.

- **The ML model is trained on synthetic point clouds.** The model, the feature
  engineering and the metrics are real; the training *data* is simulated,
  because free labelled LiDAR covering Ahmedabad does not exist. Retraining on
  classified tiles (e.g. from OpenTopography) is the next step.
- **The API ships on SQLite.** The PostGIS schema is real and verified by its
  own suite, but wiring the API to it is a two-function change in `main.py`
  that has not been made yet.
- **`read_dxf_footprint` in `ingest_real.py` is untested** — `ezdxf` was not
  available in the environment it was written in. It is marked as such in the
  source.
- **No real Ahmedabad building has been processed end to end yet.** Field
  research is underway (City Mamlatdar, City Survey, a local developer) to
  source a real sanctioned plan.
- **The viewer renders Cesium entities, not 3D Tiles.** Correct at
  single-parcel scale; 3D Tiles is the next step for true city scale.

### A bug worth mentioning

Our database verification suite caught a units error in our own schema:
volumes were being computed on EPSG:4326 **degrees** instead of projected
metres — wrong by roughly ten orders of magnitude, and it never raised an
error. Fixed in `db/schema_patch_01.sql`; `CHECK 3` in the verification suite
now guards against it permanently.

---

## Deployment

Self-hosted Docker: database, API and nginx on one host, with only port 80
exposed. This matters because government land records run in NIC and state
data centres, not on external cloud.

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** for the full walkthrough.

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline and design decisions
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — self-hosted deployment guide
- [`docs/SIH_DECK_CONTENT.md`](docs/SIH_DECK_CONTENT.md) — idea submission content
- [`docs/JUDGING_BRIEF.md`](docs/JUDGING_BRIEF.md) — presentation and Q&A preparation
- [`docs/VIDEO_SCRIPT.md`](docs/VIDEO_SCRIPT.md) — demo video script

---

## Standards

ISO 19152 (LADM) · OGC CityGML 3.0 · OGC 3D Tiles · COPC/EPT ·
OGC API – Features · DPDP Act 2023

---

## Team

**StrataCadastre_KP** — Smart India Hackathon 2026, PS 26011

## License

MIT — see [LICENSE](LICENSE).
