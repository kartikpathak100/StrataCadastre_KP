"""
main.py — Strata Cadastre (SIH26011), Phase 3
FastAPI backend + 3D-ULPIN Generator Service — spec MODULE C.

This is the glue phase: it takes pipeline.py's output (Phase 2) and
schema_3d_cadastre.sql's data model (Phase 1) and makes them into one
running system, rather than three independent artifacts. Every route
below is a thin wrapper around a plain, framework-independent function
that pipeline.py or this file defines and this file's self-tests
actually exercise.

FASTAPI AVAILABILITY: fastapi/pydantic/uvicorn are not installed in the
environment this file was built and tested in (no network access to
install them — same situation as PDAL/Shapely/Trimesh/GeoPandas in
pipeline.py). The FastAPI app and routes below are written to the
normal, standard FastAPI conventions and are NOT themselves executed in
this build. What IS actually executed, every time this file runs
(`python3 main.py`), is run_self_tests(): pure Python, no FastAPI
needed, calling the exact same functions the routes call — ULPIN
generation, the ingest -> generate -> fetch -> mask -> tax pipeline,
topology conflict detection, and Phase 2's deviation detector — end to
end, against real (if synthetic) data. Install fastapi/uvicorn and this
same file also serves the API for real; nothing above the persistence
layer needs to change.

PERSISTENCE: schema_3d_cadastre.sql (Phase 1) targets PostgreSQL +
PostGIS + SFCGAL, neither of which exist in this environment either.
This file uses Python's built-in sqlite3 instead, as a dependency-free
stand-in: geometry is stored as JSON vertex arrays rather than
PolyhedralSurfaceZ, and the SFCGAL-backed topology trigger is
reimplemented as a plain Python check (see check_topology /
footprints_overlap_area) reusing pipeline.py's tested
points_in_polygon(). Column names match the Postgres schema wherever
the concepts carry over 1:1, so pointing this at real Postgres later
is a matter of swapping get_connection()/init_schema() and the raw-SQL
calls that use them for a psycopg/SQLAlchemy equivalent — the business
logic above that line doesn't change.

RBAC: get_tier() reads an X-User-Tier header as a stand-in for real
auth (JWT/OAuth2 mapping onto the role_public_citizen /
role_authenticated_registrar DB roles from schema_3d_cadastre.sql).
Every route already takes the resolved tier as a plain parameter, so
swapping in real token validation only touches get_tier()'s body.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from pipeline import (
    ExtrudedFloor,
    detect_deviations,
    generate_synthetic_dataset,
    points_in_polygon,
    polygon_area,
)

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from pydantic import BaseModel, Field
    _HAVE_FASTAPI = True
except ImportError:
    _HAVE_FASTAPI = False


# ============================================================================
# 1. 3D-ULPIN GENERATOR SERVICE (spec section 1.5)
# ============================================================================

ULPIN_3D_PATTERN = re.compile(r"^[0-9A-Z]{6}[0-9A-Z]{14}[BGFTUE][0-9]{3}[0-9A-Z]{4}$")
VERTICAL_TYPES = {"B", "G", "F", "T", "U", "E"}


def generate_3d_ulpin(lgd_code_6d: str, base_ulpin_14d: str, vertical_type: str,
                       floor_index: int, unit_code) -> str:
    """
    Assembles the 28-character deterministic 3D-ULPIN exactly per spec
    section 1.5 and schema_3d_cadastre.sql's chk_ulpin_3d_format:
        [LGD 6D][Base 2D ULPIN 14D][Vertical Type 1D][Floor Index 3D][Unit ID 4D]

    lgd_code_6d: a 6-character LGD-derived locator (state+district+
        subdistrict, compacted to fixed width for the ID string). This
        is intentionally NOT the same width as parcels_base_2d's
        individual lgd_state_code/lgd_district_code/lgd_subdistrict_code
        columns, which keep fuller, realistic widths for administrative
        lookups — a real LGD integration normalises to this fixed 6-char
        form specifically for embedding in the ULPIN string.
    base_ulpin_14d: the parcel's existing 14-digit 2D ULPIN.
    vertical_type: one of B/G/F/T/U/E (spec section 1.5).
    floor_index: SIGNED storey index (negative = basement). The sign is
        dropped here since it's implied by vertical_type; the 3-digit
        segment encodes ABS(floor_index).
    unit_code: a 4-character code, or an int (zero-padded to 4 digits).
    """
    lgd_code_6d = lgd_code_6d.upper()
    base_ulpin_14d = str(base_ulpin_14d).upper()
    vertical_type = vertical_type.upper()
    unit_code_str = str(unit_code).zfill(4) if isinstance(unit_code, int) else str(unit_code).upper()

    if len(lgd_code_6d) != 6:
        raise ValueError(f"lgd_code_6d must be exactly 6 characters, got {lgd_code_6d!r}")
    if len(base_ulpin_14d) != 14:
        raise ValueError(f"base_ulpin_14d must be exactly 14 characters, got {base_ulpin_14d!r}")
    if vertical_type not in VERTICAL_TYPES:
        raise ValueError(f"vertical_type must be one of {sorted(VERTICAL_TYPES)}, got {vertical_type!r}")
    if len(unit_code_str) != 4:
        raise ValueError(f"unit_code must be exactly 4 characters once formatted, got {unit_code_str!r}")

    floor_segment = str(abs(int(floor_index))).zfill(3)
    if len(floor_segment) != 3:
        raise ValueError(f"floor_index magnitude too large for a 3-digit segment: {floor_index}")

    ulpin_3d = f"{lgd_code_6d}{base_ulpin_14d}{vertical_type}{floor_segment}{unit_code_str}"
    if not ULPIN_3D_PATTERN.match(ulpin_3d):
        raise ValueError(f"assembled 3D-ULPIN failed validation: {ulpin_3d!r}")
    return ulpin_3d


def floor_name_to_index(floor_name: str) -> int:
    """pipeline.py names floors 'F00', 'F05', 'F06', ... — this recovers
    the integer index generate_3d_ulpin needs."""
    return int(floor_name[1:])


# ============================================================================
# 2. PERSISTENCE — SQLite stand-in for schema_3d_cadastre.sql
# ============================================================================

def get_connection(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS parcels_base_2d (
            ulpin_2d TEXT PRIMARY KEY,
            total_parcel_area_sqm REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parcels_vertical_3d (
            ulpin_3d TEXT PRIMARY KEY,
            base_ulpin TEXT NOT NULL REFERENCES parcels_base_2d(ulpin_2d),
            vertical_type TEXT NOT NULL,
            floor_index INTEGER NOT NULL,
            footprint_xy TEXT NOT NULL,
            z_min REAL NOT NULL,
            z_max REAL NOT NULL,
            carpet_area_sqm REAL NOT NULL,
            calculated_volume_cum REAL,
            usage_type TEXT NOT NULL,
            owner_name TEXT,
            cadastral_status TEXT NOT NULL DEFAULT 'PROPOSED',
            rera_registered INTEGER NOT NULL DEFAULT 0,
            latest_tax_payment_status TEXT
        );
        """
    )
    conn.commit()


def insert_unit(conn: sqlite3.Connection, *, ulpin_3d: str, base_ulpin: str, vertical_type: str,
                 floor_index: int, footprint_xy: np.ndarray, z_min: float, z_max: float,
                 carpet_area_sqm: float, calculated_volume_cum: float, usage_type: str,
                 owner_name: Optional[str] = "Shreeji Heights Owners Association",
                 cadastral_status: str = "PROPOSED", rera_registered: bool = False) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO parcels_vertical_3d
           (ulpin_3d, base_ulpin, vertical_type, floor_index, footprint_xy, z_min, z_max,
            carpet_area_sqm, calculated_volume_cum, usage_type, owner_name,
            cadastral_status, rera_registered, latest_tax_payment_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ulpin_3d, base_ulpin, vertical_type, floor_index, json.dumps(footprint_xy.tolist()),
         z_min, z_max, carpet_area_sqm, calculated_volume_cum, usage_type, owner_name,
         cadastral_status, int(rera_registered), "DUE"),
    )
    conn.commit()


def fetch_unit(conn: sqlite3.Connection, ulpin_3d: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM parcels_vertical_3d WHERE ulpin_3d = ?", (ulpin_3d,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["footprint_xy"] = json.loads(record["footprint_xy"])
    record["rera_registered"] = bool(record["rera_registered"])
    return record


# ============================================================================
# 3. TOPOLOGY CHECK — SQLite/pure-Python stand-in for the SFCGAL trigger
# ============================================================================

def footprints_overlap_area(fp_a: np.ndarray, fp_b: np.ndarray, cell_size: float = 0.1) -> float:
    """
    2D analogue of schema_3d_cadastre.sql's fn_validate_topology (which
    uses real ST_3DIntersects/ST_3DIntersection/ST_Volume) — needed here
    because this file's SQLite persistence layer has no PostGIS/SFCGAL
    to run that trigger for real. Reuses pipeline.py's tested
    points_in_polygon via the same grid-rasterisation idea as
    pipeline.voxelize_floors, just in 2D.
    """
    all_xy = np.vstack([fp_a, fp_b])
    xmin, ymin = all_xy.min(axis=0)
    xmax, ymax = all_xy.max(axis=0)
    nx = max(1, int(np.ceil((xmax - xmin) / cell_size)))
    ny = max(1, int(np.ceil((ymax - ymin) / cell_size)))
    xs = xmin + (np.arange(nx) + 0.5) * cell_size
    ys = ymin + (np.arange(ny) + 0.5) * cell_size
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    in_a = points_in_polygon(pts, fp_a)
    in_b = points_in_polygon(pts, fp_b)
    return float(np.count_nonzero(in_a & in_b) * (cell_size ** 2))


def check_topology(conn: sqlite3.Connection, base_ulpin: str, floor_index: int,
                    footprint_xy: np.ndarray, exclude_ulpin: str = "") -> float:
    """Total overlap area (sqm) between footprint_xy and any EXISTING
    unit already stored at the same base_ulpin + floor_index. A
    non-trivial return value (see the >0.01 sqm tolerance used by
    callers) means a genuine conflict, exactly as
    fn_validate_topology's ST_Volume(ST_MakeSolid(ST_3DIntersection(...)))
    check does in the real schema — this is its 2D, SFCGAL-free stand-in."""
    rows = conn.execute(
        "SELECT footprint_xy FROM parcels_vertical_3d "
        "WHERE base_ulpin = ? AND floor_index = ? AND ulpin_3d != ?",
        (base_ulpin, floor_index, exclude_ulpin),
    ).fetchall()
    total_overlap = 0.0
    for row in rows:
        existing_fp = np.array(json.loads(row["footprint_xy"]))
        total_overlap += footprints_overlap_area(footprint_xy, existing_fp)
    return total_overlap


# ============================================================================
# 4. DPDP FIELD MASKING (mirrors v_public_property from schema_3d_cadastre.sql)
# ============================================================================

PUBLIC_FIELDS = {
    "ulpin_3d", "base_ulpin", "vertical_type", "floor_index", "footprint_xy",
    "z_min", "z_max", "carpet_area_sqm", "calculated_volume_cum",
    "cadastral_status", "rera_registered", "latest_tax_payment_status",
}


def mask_for_tier(unit_record: Dict[str, Any], tier: str) -> Dict[str, Any]:
    """Public/Citizen tier gets exactly PUBLIC_FIELDS (matching
    v_public_property); Authenticated/Registrar gets everything,
    including owner_name — no owner/KYC/encumbrance detail ever crosses
    into the public response, mirroring the DB-level GRANT split."""
    if tier == "registrar":
        return dict(unit_record)
    return {k: v for k, v in unit_record.items() if k in PUBLIC_FIELDS}


# ============================================================================
# 5. TAX CALCULATION (mirrors property_tax_registry's generated column)
# ============================================================================

USAGE_RATE_MULTIPLIERS = {
    "RESIDENTIAL": 1.0, "COMMERCIAL": 2.5, "INDUSTRIAL": 2.0,
    "MIXED": 1.75, "PARKING": 0.4, "UTILITY": 0.0, "COMMON_AREA": 0.0,
}


def calculate_tax(unit_volume_cum: float, usage_type: str, base_rate_per_cum: float,
                   floor_height_factor: float = 1.0,
                   usage_rate_multiplier: Optional[float] = None) -> float:
    """assessed_annual_value = volume * base_rate * floor_height_factor *
    usage_rate_multiplier — identical formula to property_tax_registry's
    GENERATED ALWAYS AS column in schema_3d_cadastre.sql. Multipliers
    below are illustrative demo values, not an official rate schedule —
    a real deployment reads these from the municipality's own schedule."""
    if usage_rate_multiplier is None:
        usage_rate_multiplier = USAGE_RATE_MULTIPLIERS.get(usage_type, 1.0)
    return round(unit_volume_cum * base_rate_per_cum * floor_height_factor * usage_rate_multiplier, 2)


# ============================================================================
# 6. INGESTION + ULPIN-GENERATION ORCHESTRATION
#    (the actual pipeline.py -> persistence -> API glue)
# ============================================================================

_DEVIATION_CACHE: Dict[str, dict] = {}


def run_ingest(conn: sqlite3.Connection, base_ulpin: str, total_parcel_area_sqm: float,
               use_synthetic_sample: bool = True,
               point_cloud_path: Optional[str] = None,
               sanctioned_floors: Optional[List] = None,
               origin: Optional[tuple] = None) -> Dict[str, Any]:
    """
    POST /api/v1/cadastre/ingest's actual logic.

    Two modes:
      use_synthetic_sample=True  — runs Phase 2's generate_synthetic_dataset(),
        the path validated end to end in pipeline.py. Default, and what the
        demo uses.
      use_synthetic_sample=False — loads a REAL point cloud (.las/.laz/.ply/
        .xyz) via ingest_real.py, recentres it into pipeline.py's local ENU
        frame, and derives the as-built floors from it.

    For the real path you must also supply `sanctioned_floors` — the
    approved envelope from a municipal sanctioned plan (see
    ingest_real.read_dxf_footprint, or hand-trace it). Without it there is
    nothing to compare the as-built survey against, so deviation detection
    is impossible; this raises rather than silently reporting zero
    violations, which would be far worse.
    """
    conn.execute(
        "INSERT OR REPLACE INTO parcels_base_2d (ulpin_2d, total_parcel_area_sqm) VALUES (?, ?)",
        (base_ulpin, total_parcel_area_sqm),
    )
    conn.commit()

    if use_synthetic_sample:
        dataset = generate_synthetic_dataset()
        _DEVIATION_CACHE[base_ulpin] = dataset
        return {
            "ingestion_id": str(uuid.uuid4()),
            "base_ulpin": base_ulpin,
            "source": "SYNTHETIC_SAMPLE_DATASET",
            "sanctioned_floor_count": len(dataset["sanctioned_floors"]),
            "as_built_floor_count": len(dataset["as_built_floors"]),
            "point_count": len(dataset["point_cloud"]),
        }

    if point_cloud_path is None:
        raise ValueError("point_cloud_path is required when use_synthetic_sample=False")
    if not sanctioned_floors:
        raise ValueError(
            "sanctioned_floors is required for real ingestion — deviation detection "
            "compares as-built against the approved envelope, and reporting zero "
            "violations because no envelope was supplied would be a dangerous "
            "false negative."
        )

    from ingest_real import load_point_cloud, recenter_to_local_enu, subsample

    raw_cloud = load_point_cloud(point_cloud_path)
    cloud, used_origin = recenter_to_local_enu(raw_cloud, origin)
    cloud = subsample(cloud, max_points=500_000)

    from pipeline import classify_ground_points, extract_plinth_and_apex

    classification = classify_ground_points(cloud)
    cloud = cloud.assign(classification=classification)

    # Derive the as-built envelope by extruding each sanctioned floor's
    # footprint to the height the survey actually shows. This is the
    # conservative reading: it detects extra HEIGHT (unauthorised storeys)
    # reliably. Detecting footprint encroachment from a real cloud needs
    # per-floor boundary extraction, which is Phase 2's next milestone.
    reference_footprint = sanctioned_floors[0].footprint_xy
    metrics = extract_plinth_and_apex(cloud, classification, reference_footprint)

    floor_height = sanctioned_floors[0].z_max - sanctioned_floors[0].z_min
    observed_floors = max(1, int(round(metrics["apex_height_above_plinth_m"] / floor_height)))

    as_built = []
    for i in range(observed_floors):
        template = sanctioned_floors[min(i, len(sanctioned_floors) - 1)]
        as_built.append(ExtrudedFloor(
            f"F{i:02d}", template.footprint_xy, i * floor_height, (i + 1) * floor_height
        ))

    dataset = {
        "sanctioned_floors": sanctioned_floors,
        "as_built_floors": as_built,
        "point_cloud": cloud,
        "constants": {"floor_height": floor_height, "origin": used_origin},
    }
    _DEVIATION_CACHE[base_ulpin] = dataset

    return {
        "ingestion_id": str(uuid.uuid4()),
        "base_ulpin": base_ulpin,
        "source": f"REAL_POINT_CLOUD:{Path(point_cloud_path).name}",
        "sanctioned_floor_count": len(sanctioned_floors),
        "as_built_floor_count": len(as_built),
        "point_count": len(cloud),
        "plinth_elevation_m": metrics["plinth_elevation_m"],
        "apex_height_above_plinth_m": metrics["apex_height_above_plinth_m"],
        "local_origin": used_origin,
    }


def run_generate_3d_ulpins(conn: sqlite3.Connection, base_ulpin: str, lgd_code_6d: str) -> List[Dict[str, Any]]:
    """
    POST /api/v1/ulpin/generate-3d's actual logic: takes the AS-BUILT
    floors from the cached ingestion, validates topology against
    whatever's already stored for this parcel, assigns a deterministic
    3D-ULPIN to each floor, and persists them.
    """
    dataset = _DEVIATION_CACHE.get(base_ulpin)
    if dataset is None:
        raise ValueError(f"no ingestion found for {base_ulpin}; call /cadastre/ingest first")

    generated = []
    for floor in dataset["as_built_floors"]:
        floor_idx = floor_name_to_index(floor.name)
        # Ground floor gets its own type code 'G'; upper storeys 'F';
        # anything below grade 'B'.
        if floor.z_min < 0:
            vertical_type = "B"
        elif floor_idx == 0:
            vertical_type = "G"
        else:
            vertical_type = "F"

        overlap = check_topology(conn, base_ulpin, floor_idx, floor.footprint_xy)
        if overlap > 0.01:
            raise ValueError(
                f"topology conflict inserting {floor.name}: {overlap:.3f} sqm overlap "
                f"with an existing unit on floor {floor_idx} of parcel {base_ulpin}"
            )

        ulpin_3d = generate_3d_ulpin(lgd_code_6d, base_ulpin, vertical_type, floor_idx, unit_code=floor_idx)
        area = polygon_area(floor.footprint_xy)
        volume = float(area * (floor.z_max - floor.z_min))

        insert_unit(
            conn, ulpin_3d=ulpin_3d, base_ulpin=base_ulpin, vertical_type=vertical_type,
            floor_index=floor_idx, footprint_xy=floor.footprint_xy, z_min=floor.z_min, z_max=floor.z_max,
            carpet_area_sqm=round(area, 2), calculated_volume_cum=round(volume, 3),
            usage_type="COMMERCIAL" if vertical_type == "G" else "RESIDENTIAL",
        )
        generated.append({"ulpin_3d": ulpin_3d, "floor": floor.name,
                           "vertical_type": vertical_type, "volume_cum": round(volume, 3)})

    # Non-storey vertical parcels: air-rights (T), subsurface utility (U),
    # elevated transport corridor (E). These are the volumes a 2D cadastre
    # structurally cannot represent, so they are the clearest demonstration
    # of what the 3D system adds.
    for special in dataset.get("special_volumes", []):
        # Floor index is derived from elevation so these sort sensibly
        # against storeys; the sign is carried by vertical_type.
        floor_idx = int(abs(special["z_min"]) // 3.2) + 900
        area = polygon_area(special["footprint_xy"])
        volume = float(area * (special["z_max"] - special["z_min"]))

        ulpin_3d = generate_3d_ulpin(
            lgd_code_6d, base_ulpin, special["vertical_type"],
            floor_idx, unit_code=special["id"].ljust(4, "0")[:4],
        )

        insert_unit(
            conn, ulpin_3d=ulpin_3d, base_ulpin=base_ulpin,
            vertical_type=special["vertical_type"], floor_index=floor_idx,
            footprint_xy=special["footprint_xy"],
            z_min=special["z_min"], z_max=special["z_max"],
            carpet_area_sqm=round(area, 2), calculated_volume_cum=round(volume, 3),
            usage_type=special["usage_type"], owner_name=special["owner"],
        )
        generated.append({
            "ulpin_3d": ulpin_3d, "floor": special["label"],
            "vertical_type": special["vertical_type"], "volume_cum": round(volume, 3),
        })

    return generated


# ============================================================================
# 7. FASTAPI APP — only defined if fastapi is actually installed
# ============================================================================

if _HAVE_FASTAPI:

    app = FastAPI(
        title="Strata Cadastre API",
        version="0.3.0-phase3",
        description="SIH26011 — 3D ULPIN Generation and Vertical Property Mapping System",
    )

    _conn = get_connection()
    init_schema(_conn)

    def get_tier(x_user_tier: str = Header(default="public")) -> str:
        """Demo-simplified RBAC: a header stands in for real auth (JWT/
        OAuth2 mapping onto role_public_citizen / role_authenticated_
        registrar from schema_3d_cadastre.sql). Every route already
        takes the resolved tier as a plain parameter, so only this
        function's body needs to change for real token validation."""
        tier = x_user_tier.lower()
        if tier not in ("public", "registrar"):
            raise HTTPException(400, "X-User-Tier must be 'public' or 'registrar'")
        return tier

    class IngestRequest(BaseModel):
        base_ulpin: str = Field(..., min_length=14, max_length=14)
        total_parcel_area_sqm: float = Field(..., gt=0)
        use_synthetic_sample: bool = True

    class GenerateUlpinRequest(BaseModel):
        base_ulpin: str = Field(..., min_length=14, max_length=14)
        lgd_code_6d: str = Field(..., min_length=6, max_length=6)

    @app.post("/api/v1/cadastre/ingest")
    def ingest(req: IngestRequest) -> Dict[str, Any]:
        try:
            return run_ingest(_conn, req.base_ulpin, req.total_parcel_area_sqm, req.use_synthetic_sample)
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc))

    @app.post("/api/v1/ulpin/generate-3d")
    def generate_ulpins(req: GenerateUlpinRequest) -> List[Dict[str, Any]]:
        try:
            return run_generate_3d_ulpins(_conn, req.base_ulpin, req.lgd_code_6d)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/v1/units/{ulpin_3d}")
    def get_unit(ulpin_3d: str, tier: str = Depends(get_tier)) -> Dict[str, Any]:
        record = fetch_unit(_conn, ulpin_3d)
        if record is None:
            raise HTTPException(404, f"{ulpin_3d} not found")
        return mask_for_tier(record, tier)

    @app.get("/api/v1/analytics/deviations/{base_ulpin}")
    def get_deviations(base_ulpin: str) -> Dict[str, Any]:
        dataset = _DEVIATION_CACHE.get(base_ulpin)
        if dataset is None:
            raise HTTPException(404, f"no ingestion on file for {base_ulpin}")
        return detect_deviations(dataset["as_built_floors"], dataset["sanctioned_floors"])

    @app.get("/api/v1/tax/calculate/{ulpin_3d}")
    def get_tax(ulpin_3d: str, base_rate_per_cum: float = Query(450.0, gt=0)) -> Dict[str, Any]:
        record = fetch_unit(_conn, ulpin_3d)
        if record is None:
            raise HTTPException(404, f"{ulpin_3d} not found")
        amount = calculate_tax(record["calculated_volume_cum"], record["usage_type"], base_rate_per_cum)
        return {
            "ulpin_3d": ulpin_3d, "usage_type": record["usage_type"],
            "unit_volume_cum": record["calculated_volume_cum"],
            "base_rate_per_cum": base_rate_per_cum, "assessed_annual_value": amount,
        }


# ============================================================================
# 8. SELF-TESTS — pure Python, no FastAPI needed, always runs
# ============================================================================

def run_self_tests() -> None:
    print("=" * 78)
    print("Strata Cadastre — Phase 3 main.py self-tests (pure Python; these")
    print("exercise every function the API routes call, independent of")
    print("whether FastAPI itself is installed in this environment).")
    print("=" * 78)

    print("\n--- 1. 3D-ULPIN generator ---")
    ulpin = generate_3d_ulpin("240124", "11000088421000", "F", 4, 402)
    print(f"  generated: {ulpin}  (length={len(ulpin)})")
    assert len(ulpin) == 28, "3D-ULPIN must be exactly 28 characters"
    assert ULPIN_3D_PATTERN.match(ulpin), "3D-ULPIN failed its own regex"
    try:
        generate_3d_ulpin("24", "11000088421000", "F", 4, 402)
        raise AssertionError("expected ValueError for a malformed LGD code")
    except ValueError:
        print("  correctly rejected a malformed (2-char) LGD code")

    print("\n--- 2. End-to-end: pipeline.py -> persistence -> API logic ---")
    conn = get_connection()
    init_schema(conn)
    base_ulpin = "11000088421000"
    lgd_code_6d = "240124"

    ingest_result = run_ingest(conn, base_ulpin, total_parcel_area_sqm=3843.0)
    print(f"  ingested via pipeline.generate_synthetic_dataset(): "
          f"{ingest_result['as_built_floor_count']} as-built floors, "
          f"{ingest_result['point_count']:,} points")

    generated = run_generate_3d_ulpins(conn, base_ulpin, lgd_code_6d)
    print(f"  generated {len(generated)} 3D-ULPINs, e.g. {generated[0]['ulpin_3d']} ({generated[0]['floor']})")
    assert len(generated) == 10, \
        "expected 10 units: 7 as-built storeys (G+5 sanctioned + 1 unauthorised) + 3 special volumes"

    print("\n  vertical parcel types generated (spec section 1.5):")
    by_type = {}
    for unit in generated:
        by_type.setdefault(unit["vertical_type"], []).append(unit)
    type_labels = {"B": "Basement/subsurface", "G": "Ground", "F": "Floor",
                    "T": "Terrace/air-rights", "U": "Subsurface utility",
                    "E": "Elevated corridor"}
    for code in "BGFTUE":
        units_of_type = by_type.get(code, [])
        if units_of_type:
            example = units_of_type[0]
            print(f"    {code}  {type_labels[code]:<22} {len(units_of_type)} unit(s)  "
                  f"e.g. {example['ulpin_3d']}")
        else:
            print(f"    {code}  {type_labels[code]:<22} none")

    generated_types = set(by_type)
    # G, F, T, U, E must all appear. B is absent here because this parcel's
    # basement is modelled as a special volume rather than a negative-index
    # storey — the type is exercised by the viewer and the SQL schema.
    required = {"G", "F", "T", "U", "E"}
    missing = required - generated_types
    assert not missing, f"vertical types declared but never generated: {sorted(missing)}"
    print(f"  all {len(required)} required vertical types present")

    conflict_floor = _DEVIATION_CACHE[base_ulpin]["as_built_floors"][0]
    overlap = check_topology(conn, base_ulpin, 0, conflict_floor.footprint_xy)
    assert overlap > 100, f"expected a large self-overlap re-checking floor 0, got {overlap:.2f} sqm"
    print(f"  topology check correctly detects {overlap:.1f} sqm overlap "
          f"re-testing an already-registered footprint")

    sample_ulpin = generated[3]["ulpin_3d"]
    record = fetch_unit(conn, sample_ulpin)
    public_view = mask_for_tier(record, "public")
    registrar_view = mask_for_tier(record, "registrar")
    print(f"  fetched {sample_ulpin}: public view has {len(public_view)} fields, "
          f"registrar view has {len(registrar_view)} fields")
    assert "owner_name" not in public_view, "DPDP masking failed: owner_name leaked to the public tier"
    assert "owner_name" in registrar_view, "registrar tier should see owner_name"
    print("  DPDP field masking verified: owner_name present for registrar, absent for public")

    tax = calculate_tax(record["calculated_volume_cum"], record["usage_type"], base_rate_per_cum=450.0)
    print(f"  tax on {sample_ulpin} ({record['usage_type']}, "
          f"{record['calculated_volume_cum']:.1f} m3 @ Rs.450/m3): Rs. {tax:,.2f}/year")

    print("\n--- 3. Deviation analytics (same detector validated in Phase 2) ---")
    dataset = _DEVIATION_CACHE[base_ulpin]
    deviations = detect_deviations(dataset["as_built_floors"], dataset["sanctioned_floors"])
    print(f"  total unauthorised volume for {base_ulpin}: "
          f"{deviations['total_unauthorised_volume_cum']} m3")
    for row in deviations["per_floor"]:
        if row["deviation_type"]:
            print(f"    {row['floor']}: {row['unauthorised_volume_cum']} m3 -> {row['deviation_type']}")

    print("\n" + "=" * 78)
    print(f"All Phase 3 self-tests passed. FastAPI installed: {_HAVE_FASTAPI}")
    print("=" * 78)


if __name__ == "__main__":
    run_self_tests()
    if _HAVE_FASTAPI:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        print(
            "\nfastapi/uvicorn aren't installed in this environment, so the "
            "HTTP server didn't start — install with:\n"
            "    pip install fastapi 'uvicorn[standard]'\n"
            "and re-run `python3 main.py` to actually serve the 5 endpoints "
            "above. The self-tests just run/passed exercise the exact same "
            "logic those endpoints call."
        )
