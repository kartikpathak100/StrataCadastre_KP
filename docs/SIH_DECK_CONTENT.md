# Strata Cadastre — SIH Idea Submission Deck Content

**PS ID:** SIH26011 · **Title:** 3D ULPIN Generation and Vertical Property Mapping System
**Ministry/Org:** Ministry of Rural Development — Department of Land Resources (DoLR)
**Theme:** Smart Automation / GIS & Urban Land Governance · **Category:** Software

---

## Read this first — three template rules that fail submissions

1. **Six slides maximum, including the title slide.** Not six content slides.
2. **Use the official template as downloaded.** Do not delete or reword its
   built-in section pointers. You may delete the "Important Pointers"
   instruction slide before uploading.
3. **Upload as PDF.** PPT, DOCX and every other format is rejected outright.

Also: the template wants **points, diagrams and infographics — not
paragraphs**. Everything below is written as short bullets for that reason.
If a bullet feels long, cut it rather than shrinking the font.

**Note on slide order:** the official template's slide 6 is *Research and
References*. Team details belong on the **title slide**, not a closing slide.
Content below follows the official structure.

---

# SLIDE 1 — Title

**Fill the template's own fields:**

| Field | Value |
|---|---|
| Problem Statement ID | SIH26011 |
| Problem Statement Title | 3D ULPIN Generation and Vertical Property Mapping System |
| Theme | Smart Automation / GIS & Urban Land Governance |
| PS Category | Software |
| Team ID | `[YOUR TEAM ID]` |
| Team Name | `[YOUR TEAM NAME]` |

**Add a one-line positioning statement under the title:**

> Strata Cadastre — giving every flat, basement and air-rights volume its own
> legally distinct 3D identity.

---

# SLIDE 2 — Proposed Solution

### The gap (2–3 bullets, keep it tight)

- India's land record system identifies **parcels of ground**. A 12-storey
  building is one parcel — 24 flats share a single 2D ULPIN.
- No unique spatial identity exists for basements, parking bays, metro
  tunnels, elevated corridors, or air rights above a plot.
- Consequence: overlapping claims cannot be *detected*, only litigated after
  the fact.

### The solution

- **3D Volumetric Cadastre** — every unit stored as a true closed volume
  (`PolyhedralSurfaceZ`), not a polygon with a floor number attached.
- **28-character 3D-ULPIN**, deterministically generated:
  `[LGD 6][Base 2D ULPIN 14][Vertical Type 1][Floor 3][Unit 4]`
  Vertical types: **B**asement · **G**round · **F**loor · **T**errace/air-rights
  · **U**tility · **E**levated corridor.
- **AI/ML multi-source fusion** — drone orthomosaics, LiDAR point clouds and
  sanctioned CAD/IFC plans reconciled into one validated building model.
- **Automatic topology validation** — the database physically *refuses* to
  register a unit that overlaps an existing one.

### Innovation and uniqueness

- **Legally correct, not just geometrically correct.** Models Indian
  apartment law as it actually works: *Undivided Share in Land (UDS)* on the
  base parcel **plus** exclusive right to a spatial volume — two linked
  records, not one flattened row.
- **Fusion over pure computer vision.** Floor count is never inferred from
  height alone (double-height lobbies and mezzanines break that); LiDAR nDSM
  is reconciled against sanctioned plans, and mismatches are *flagged*, not
  silently resolved.
- **Detects unauthorised construction as a by-product.** ΔV = as-built minus
  sanctioned volume — the same data that assigns ULPINs also finds the extra
  floor.

**Visual:** one 2D parcel with 24 overlapping flat-claims on the left → the
same building as a stacked 3D volume tower with distinct IDs on the right.

---

# SLIDE 3 — Technical Approach

### Technologies

| Layer | Stack |
|---|---|
| Ingestion | Drone GeoTIFF · LiDAR LAS/LAZ · DEM/DSM · CAD/IFC · GNSS-CORS |
| Processing | Python · PDAL · GDAL · Shapely · Trimesh · NumPy/SciPy |
| Geodesy | Ellipsoidal → orthometric height (**H = h − N**) |
| Database | PostgreSQL + **PostGIS 3D + SFCGAL** |
| Backend | FastAPI (async REST, OGC API–Features aligned) |
| Frontend | React + CesiumJS |
| Deployment | Docker · nginx · self-hosted (NIC/on-prem compatible) |

### Pipeline

```
Drone · LiDAR · CAD/IFC · GNSS-CORS · DEM/DSM
                │
                ▼
   GEODETIC NORMALISATION      H = h − N  (geoid undulation)
                │              absolute + plinth-relative elevation
                ▼
   AI/ML FUSION ENGINE         footprint extraction + relief correction
                │              nDSM (DSM−DEM) reconciled with sanctioned CAD
                │              ICP alignment · sensor precedence resolution
                ▼
   TOPOLOGY + DEVIATION        zero-overlap enforcement
                │              ΔV = as-built \ sanctioned
                ▼
   3D-ULPIN ASSIGNMENT         28-char deterministic identifier
                │
                ▼
   PostGIS 3D  →  FastAPI  →  CesiumJS 3D viewer
```

### Sensor precedence (conflict resolution rule)

**CORS-RTK GNSS (±10 cm) > LiDAR > Sanctioned CAD > Legacy 2D GIS**
Every spatial node carries `source_confidence_score` and
`positional_uncertainty_cm` — provenance travels with the geometry.

**Visual:** the pipeline block above, drawn horizontally with icons.

---

# SLIDE 4 — Feasibility and Viability

### Why it is feasible now

- Built entirely on **mature open standards** — no unproven technology.
- **Working prototype already running**, with measured results:

| Component | Validated result |
|---|---|
| Deviation detector | within **1.56%** of analytic ground truth |
| ICP plan alignment | 12° rotation recovered to **0.022°**, translation to **1 mm** |
| Plinth / apex extraction | **±1 cm** / **±7 cm** from LiDAR |
| Database integrity | **9-check** automated verification suite |

- Runs on commodity hardware; **self-hosted**, so it deploys inside existing
  government data centres rather than requiring external cloud.

### Interoperability

- **ISO 19152 (LADM)** — the international land administration standard.
- **OGC CityGML 3.0 LOD2/LOD3** · **OGC 3D Tiles** · **COPC/EPT** streaming.
- Consumes existing **2D ULPINs** — extends DoLR records, never replaces them.
- Integrates with state portals (**AnyRoR**, Bhu-Naksha, DILRMP).
- **DPDP Act 2023** compliant: tiered RBAC — citizens see volume, ULPIN and
  RERA status; registrars see owner KYC, deeds and encumbrances.

### Challenges and mitigations

| Risk | Mitigation |
|---|---|
| LiDAR/drone data unavailable for most parcels | Degrades gracefully to CAD + GNSS; precedence matrix handles missing sources |
| Legacy 2D records inaccurate | Provenance + uncertainty stored per node; never silently overwritten |
| Geoid model accuracy | Pluggable — swap regional table for Survey of India geoid |
| Municipal adoption effort | Extends existing ULPIN; no re-survey required to start |

---

# SLIDE 5 — Impact and Benefits

### Social

- **Cuts the root cause of apartment litigation** — overlapping claims become
  impossible to *register*, not merely arguable in court.
- Flat buyers get a verifiable spatial identity, RERA status and encumbrance
  check before purchase.
- Basement, parking and terrace rights become individually titled — today
  they are among the most disputed and least documented assets in Indian
  housing.

### Economic

- **Volumetric property tax** — assessment on actual cubic metres and usage,
  replacing flat per-unit rates. Broadens the municipal base without raising
  rates.
- **Automated unauthorised-construction detection** recovers regularisation
  revenue and reduces manual survey cost.
- Cleaner titles reduce lending risk and mortgage disputes.

### Governance and infrastructure

- Utilities, metro tunnels and elevated corridors get registered subsurface
  and airspace rights — preventing the excavation strikes and alignment
  disputes that delay urban infrastructure.
- Enables genuine 3D urban planning: FSI, setbacks and air-rights analysed
  volumetrically.

**Visual:** a single icon row — Citizen · Registrar · Municipality · Utility
— each with its one-line benefit.

---

# SLIDE 6 — Research and References

Keep to 5–7 entries. Paste the exact URLs from the official sources.

- **ISO 19152:2012 — Land Administration Domain Model (LADM)**, ISO.
- **OGC CityGML 3.0 Conceptual Model Standard**, Open Geospatial Consortium.
- **OGC 3D Tiles Specification**, Open Geospatial Consortium.
- **ULPIN / Digital India Land Records Modernisation Programme (DILRMP)**,
  Department of Land Resources, Ministry of Rural Development.
- **SVAMITVA Scheme** — drone-based survey of inhabited rural areas, DoLR.
- **Digital Personal Data Protection Act, 2023**, Government of India.
- **PostGIS 3D / SFCGAL documentation** — volumetric spatial operations.
- Applicable **State Apartment Ownership Act** (e.g. Gujarat Ownership
  Flats Act) for the UDS + exclusive-volume ownership model.

> Verify every link before submitting. A dead reference is the easiest
> possible thing for a judge to catch.

---

## Design notes

- **One idea per slide.** The template rewards clarity over density.
- **Diagrams beat text.** Slides 2, 3 and 5 should each be roughly half visual.
- Keep the palette to two colours plus grey. Avoid stock "smart city" clipart.
- Put the **measured numbers** (1.56%, 0.022°, ±1 cm) on the slide, not just
  in your speech — a number on screen reads as evidence.

## If you get a live Q&A

The strongest answer you have to *"how do you know it works?"* is:

> We wrote an automated verification suite for our own database — and it
> caught a units error in our schema where volumes were being computed in
> degrees instead of metres, wrong by ten orders of magnitude. We fixed it
> and added a permanent check for it.

That is a testing story, not a feature claim. Very few teams will have one.
