-- ============================================================================
-- schema_3d_cadastre.sql — Strata Cadastre (SIH26011), Phase 1 / MODULE B
-- PostgreSQL 14+ / PostGIS 3.3+ with the SFCGAL backend compiled in.
--
-- Scope: parcels_base_2d, parcels_vertical_3d, cadastral_lifecycle,
-- property_tax_registry, encumbrance_registry — the five tables named in
-- the brief — plus two small additions needed to make the rest of the
-- brief's own requirements actually work end to end:
--   * owner_registry          (spec section 1.7 names "Owner KYC, Sale
--                               Deed Hash" as registrar-tier data, but no
--                               table was named to hold it)
--   * point_cloud_references  (spec section 6 requires raw LAS/LAZ point
--                               clouds to live in COPC/EPT object storage,
--                               never in the relational DB — this table is
--                               the pointer, not the payload)
--
-- A NAMING CORRECTION UP FRONT: the brief asks for "ST_3DVolume". PostGIS's
-- SQL/MM-compliant volume function is actually called ST_Volume (it
-- implements the SQL/MM spec function that standard calls ST_3DVolume,
-- but there is no function literally named ST_3DVolume in PostGIS). It
-- also only returns a non-zero result on a genuine SOLID, so every volume
-- call below wraps the geometry in ST_MakeSolid() first. ST_Volume itself
-- is deprecated as of PostGIS 3.5 in favour of CG_Volume with the same
-- signature — if you're on 3.5+, do a find-replace. Every other function
-- named in the brief (ST_3DIntersects, ST_3DIntersection, ST_3DArea,
-- ST_ZMin, ST_ZMax) is real and used under its literal name below.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 0. EXTENSIONS & CRS STRATEGY
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;
-- postgis_sfcgal powers ST_3DIntersection, ST_3DArea, ST_Volume,
-- ST_MakeSolid and ST_IsSolid below.

-- spatial_ref_sys is populated automatically by CREATE EXTENSION postgis
-- and already contains every SRID this schema uses, so no manual inserts
-- are needed for this build:
--   EPSG:4326  WGS 84 (lat/lon)       — storage & interchange CRS, matches
--                                       raw GNSS/CORS output and the 2D
--                                       ULPIN system.
--   EPSG:32643 WGS 84 / UTM zone 43N  — the working *projected* CRS for
--                                       any planar-metric math (Ahmedabad/
--                                       Gujarat falls in zone 43N;
--                                       re-project with ST_Transform(geom,
--                                       32643) before trusting a raw
--                                       ST_Area in square metres on 4326
--                                       data).
-- If a state deploys against its own legacy/Everest-based revenue-map
-- grid, register that SRID with INSERT INTO spatial_ref_sys (...) here
-- and thread it through the *_srid columns rather than hardcoding 4326.
--
-- Z-axis note: geometry Z values throughout this schema are plain metres
-- in an arbitrary local vertical reference (whatever the ingestion
-- pipeline extruded from), NOT a geodetic height by themselves. The two
-- fields that ARE geodetically meaningful — orthometric_base_z_m /
-- orthometric_top_z_m and the relative_plinth_* columns — are computed by
-- geodesy.py and stored as plain NUMERIC columns rather than encoded into
-- geometry Z, precisely so "the shape" and "the legally authoritative
-- elevation of record" can't silently drift apart or get confused for
-- each other.


-- ----------------------------------------------------------------------------
-- 1. parcels_base_2d — the 2D ULPIN base parcel (spec section 1.4: this is
-- the "Undivided Share in Land" side of the ownership model)
-- ----------------------------------------------------------------------------
CREATE TABLE parcels_base_2d (
    id BIGSERIAL PRIMARY KEY,
    ulpin_2d CHAR(14) NOT NULL UNIQUE, -- DoLR 14-digit ULPIN
    lgd_state_code VARCHAR(2) NOT NULL,
    lgd_district_code VARCHAR(4) NOT NULL,
    lgd_subdistrict_code VARCHAR(6) NOT NULL,
    lgd_village_code VARCHAR(6),
    survey_number VARCHAR(30),
    total_parcel_area_sqm NUMERIC(12,2) NOT NULL CHECK (total_parcel_area_sqm > 0),
    boundary_2d GEOMETRY(MultiPolygon, 4326) NOT NULL,
    cors_benchmark_point GEOMETRY(PointZ, 4326), -- Z = raw ellipsoidal height (h), NOT orthometric
    cors_benchmark_station_id VARCHAR(20),
    source_confidence_score NUMERIC(4,3) CHECK (source_confidence_score BETWEEN 0 AND 1),
    positional_uncertainty_cm NUMERIC(6,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_ulpin_2d_format CHECK (ulpin_2d ~ '^[0-9]{14}$'),
    CONSTRAINT chk_boundary_2d_valid CHECK (ST_IsValid(boundary_2d))
);

COMMENT ON TABLE parcels_base_2d IS
  'Base 2D cadastral parcel (Undivided Share in Land). One row per legacy/'
  '2D ULPIN. Every parcels_vertical_3d unit hangs off exactly one of these.';


-- ----------------------------------------------------------------------------
-- 2. parcels_vertical_3d — the 3D-ULPIN spatial unit (spec sections 1.4,
-- 1.5: the "Exclusive Right to Possession of the Spatial Volume" side)
-- ----------------------------------------------------------------------------
CREATE TABLE parcels_vertical_3d (
    id BIGSERIAL PRIMARY KEY,
    ulpin_3d CHAR(28) NOT NULL UNIQUE, -- spec 1.5, 28-char id
    base_ulpin CHAR(14) NOT NULL REFERENCES parcels_base_2d(ulpin_2d) ON UPDATE CASCADE,
    vertical_type CHAR(1) NOT NULL CHECK (vertical_type IN ('B','G','F','T','U','E')),
    -- B=Basement/Subsurface  G=Ground  F=Floor  T=Terrace/Air-Rights
    -- U=Subsurface Utility   E=Elevated Corridor
    floor_index SMALLINT NOT NULL,
    -- SIGNED: negative = basement level, 0 = ground, +N = floor N. (The
    -- 3-digit floor segment inside ulpin_3d itself is UNSIGNED — sign is
    -- implied by vertical_type there.)
    unit_code VARCHAR(4) NOT NULL,
    geometry_3d GEOMETRY(PolyhedralSurfaceZ, 4326) NOT NULL,
    orthometric_base_z_m NUMERIC(8,3) NOT NULL, -- absolute_geodetic_elevation, unit base
    orthometric_top_z_m NUMERIC(8,3) NOT NULL, -- absolute_geodetic_elevation, unit top
    relative_plinth_base_m NUMERIC(8,3), -- relative_plinth_elevation, unit base
    relative_plinth_top_m NUMERIC(8,3), -- relative_plinth_elevation, unit top
    calculated_volume_cum NUMERIC(12,3), -- populated by trg_..._metrics below
    carpet_area_sqm NUMERIC(10,2) NOT NULL CHECK (carpet_area_sqm > 0),
    uds_percentage NUMERIC(6,4) CHECK (uds_percentage BETWEEN 0 AND 100),
    source_confidence_score NUMERIC(4,3) CHECK (source_confidence_score BETWEEN 0 AND 1),
    positional_uncertainty_cm NUMERIC(6,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_ulpin_3d_format
        CHECK (ulpin_3d ~ '^[0-9A-Z]{6}[0-9A-Z]{14}[BGFTUE][0-9]{3}[0-9A-Z]{4}$'),
    CONSTRAINT chk_z_order CHECK (orthometric_top_z_m > orthometric_base_z_m),
    CONSTRAINT chk_geometry_3d_closed
        CHECK (ST_IsClosed(geometry_3d) OR ST_IsEmpty(geometry_3d))
);

COMMENT ON TABLE parcels_vertical_3d IS
  '3D-ULPIN spatial unit: an apartment, basement bay, terrace/air-rights '
  'volume, subsurface utility run, or elevated corridor segment. One row '
  'per volumetrically distinct, individually identifiable unit.';
COMMENT ON COLUMN parcels_vertical_3d.floor_index IS
  'Signed storey index for querying (negative = below grade). The '
  'unsigned 3-digit floor segment encoded inside ulpin_3d is derived '
  'from ABS(floor_index) at generation time in the Phase 3 ULPIN service.';


-- ----------------------------------------------------------------------------
-- 3. cadastral_lifecycle — append-only state-machine log (spec section
-- 2.B.1: mutation history + trigger reference)
-- ----------------------------------------------------------------------------
CREATE TABLE cadastral_lifecycle (
    id BIGSERIAL PRIMARY KEY,
    ulpin_3d CHAR(28) NOT NULL REFERENCES parcels_vertical_3d(ulpin_3d)
        ON UPDATE CASCADE ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('PROPOSED','UNDER_CONSTRUCTION','OC_ISSUED_ACTIVE','DISPUTED','DEMOLISHED')),
    previous_status VARCHAR(20), -- filled in by the transition trigger, not by callers
    trigger_reference_type VARCHAR(30),
    -- e.g. 'MUNICIPAL_OC_CERTIFICATE', 'COURT_CASE_ID', 'DEMOLITION_ORDER', 'SANCTION_REVOCATION'
    trigger_reference_id VARCHAR(60),
    remarks TEXT,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by VARCHAR(60),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE cadastral_lifecycle IS
  'Append-only event log — the CURRENT status of a unit is its latest row '
  '(see v_current_cadastral_status). Never UPDATE a historical row.';


-- ----------------------------------------------------------------------------
-- 4. property_tax_registry (spec section 2.B.1: assessed annual rateable
-- value from unit volume, floor-height factor, usage type)
-- ----------------------------------------------------------------------------
CREATE TABLE property_tax_registry (
    id BIGSERIAL PRIMARY KEY,
    ulpin_3d CHAR(28) NOT NULL REFERENCES parcels_vertical_3d(ulpin_3d)
        ON UPDATE CASCADE ON DELETE CASCADE,
    assessment_year SMALLINT NOT NULL,
    usage_type VARCHAR(20) NOT NULL
        CHECK (usage_type IN ('RESIDENTIAL','COMMERCIAL','INDUSTRIAL','MIXED','PARKING','UTILITY','COMMON_AREA')),
    unit_volume_cum NUMERIC(12,3) NOT NULL CHECK (unit_volume_cum >= 0),
    floor_height_factor NUMERIC(5,3) NOT NULL DEFAULT 1.000,
    -- municipal multiplier, e.g. double-height lobbies / premium floors
    usage_rate_multiplier NUMERIC(5,3) NOT NULL DEFAULT 1.000, -- municipal per-usage-type multiplier
    base_rate_per_cum NUMERIC(10,4) NOT NULL CHECK (base_rate_per_cum >= 0), -- Rs. per cubic metre
    assessed_annual_value NUMERIC(14,2) GENERATED ALWAYS AS (
        unit_volume_cum * base_rate_per_cum * floor_height_factor * usage_rate_multiplier
    ) STORED,
    payment_status VARCHAR(15) NOT NULL DEFAULT 'DUE'
        CHECK (payment_status IN ('DUE','PAID','OVERDUE','WAIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ulpin_3d, assessment_year)
);

COMMENT ON TABLE property_tax_registry IS
  'One row per unit per assessment year. assessed_annual_value is a '
  'STORED generated column (pure arithmetic, safe for GENERATED ALWAYS — '
  'unlike the spatial metrics on parcels_vertical_3d, which need a '
  'trigger instead; see the note above trg_parcels_vertical_3d_metrics).';


-- ----------------------------------------------------------------------------
-- 5. encumbrance_registry (spec section 2.B.1: bank liens, RERA
-- registration numbers, active litigation flags)
-- ----------------------------------------------------------------------------
CREATE TABLE encumbrance_registry (
    id BIGSERIAL PRIMARY KEY,
    ulpin_3d CHAR(28) NOT NULL REFERENCES parcels_vertical_3d(ulpin_3d)
        ON UPDATE CASCADE ON DELETE CASCADE,
    encumbrance_type VARCHAR(20) NOT NULL
        CHECK (encumbrance_type IN ('BANK_LIEN','RERA_REGISTRATION','LITIGATION','MORTGAGE','EASEMENT','OTHER')),
    reference_number VARCHAR(60) NOT NULL,
    lender_or_authority VARCHAR(120),
    amount_secured NUMERIC(14,2),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    filed_on DATE NOT NULL DEFAULT CURRENT_DATE,
    resolved_on DATE,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (resolved_on IS NULL OR resolved_on >= filed_on)
);

COMMENT ON TABLE encumbrance_registry IS
  'Bank liens, mortgages, RERA registrations, and litigation flags. '
  'RERA_REGISTRATION rows with is_active are what v_public_property '
  'surfaces as "RERA registered" to the public tier.';


-- ----------------------------------------------------------------------------
-- 6. owner_registry — NOT explicitly named among the five MODULE B
-- tables, but required for section 1.7's RBAC tiers ("Owner KYC, Sale
-- Deed Hash" for the registrar tier) to mean anything. DPDP-minded by
-- construction: raw identity-document numbers are never stored, only a
-- verification status and a hash/reference to the record held by the
-- verifying authority.
-- ----------------------------------------------------------------------------
CREATE TABLE owner_registry (
    id BIGSERIAL PRIMARY KEY,
    ulpin_3d CHAR(28) NOT NULL REFERENCES parcels_vertical_3d(ulpin_3d)
        ON UPDATE CASCADE ON DELETE CASCADE,
    owner_name VARCHAR(150) NOT NULL,
    ownership_share_percent NUMERIC(6,4) NOT NULL DEFAULT 100.0000
        CHECK (ownership_share_percent > 0 AND ownership_share_percent <= 100),
    kyc_verification_status VARCHAR(15) NOT NULL DEFAULT 'PENDING'
        CHECK (kyc_verification_status IN ('PENDING','VERIFIED','REJECTED')),
    kyc_reference_hash VARCHAR(128),
    -- hash/reference to the KYC transaction held by the verifying
    -- authority (e.g. DigiLocker/e-KYC txn id) — never a raw Aadhaar/PAN number.
    sale_deed_hash VARCHAR(128), -- hash of the registered sale-deed document
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    ownership_from DATE NOT NULL DEFAULT CURRENT_DATE,
    ownership_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ownership_to IS NULL OR ownership_to >= ownership_from)
);

COMMENT ON TABLE owner_registry IS
  'Current + historical ownership. Registrar-tier only (see v_public_'
  'property, which never selects from this table).';


-- ----------------------------------------------------------------------------
-- 7. point_cloud_references — implements spec section 6's "do NOT store
-- raw .las point clouds relationally; stream via COPC/EPT" as an actual
-- constraint: this table holds pointers, never point data.
-- ----------------------------------------------------------------------------
CREATE TABLE point_cloud_references (
    id BIGSERIAL PRIMARY KEY,
    base_ulpin CHAR(14) NOT NULL REFERENCES parcels_base_2d(ulpin_2d)
        ON UPDATE CASCADE ON DELETE CASCADE,
    source_type VARCHAR(10) NOT NULL CHECK (source_type IN ('COPC','EPT')),
    endpoint_url TEXT NOT NULL, -- .copc.laz URL or ept.json root, on object storage / a tile server
    capture_date DATE,
    point_count_estimate BIGINT,
    crs_epsg INTEGER NOT NULL DEFAULT 4326,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE point_cloud_references IS
  'Pointers to externally-hosted COPC/EPT point cloud data, never the '
  'raw points themselves. Phase 2''s pipeline.py streams from here.';


-- ============================================================================
-- TRIGGERS
-- ============================================================================

-- Generic updated_at toucher, reused across tables that have the column.
CREATE OR REPLACE FUNCTION fn_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_parcels_base_2d_touch
    BEFORE UPDATE ON parcels_base_2d
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_parcels_vertical_3d_touch
    BEFORE UPDATE ON parcels_vertical_3d
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();


-- ----------------------------------------------------------------------------
-- Spatial metrics: populate calculated_volume_cum on write. This is a
-- TRIGGER, not a GENERATED ALWAYS AS ... STORED column, because Postgres
-- only allows IMMUTABLE expressions in stored generated columns and the
-- SFCGAL-backed spatial functions are STABLE, not IMMUTABLE. Wrapped in
-- an exception handler so a not-yet-closed or otherwise not-solid-able
-- surface doesn't hard-fail the write — it just leaves the metric NULL
-- for the pipeline to reconcile once the geometry is finalised.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_compute_geometry_metrics()
RETURNS TRIGGER AS $$
BEGIN
    BEGIN
        NEW.calculated_volume_cum := ST_Volume(ST_MakeSolid(NEW.geometry_3d));
    EXCEPTION WHEN OTHERS THEN
        NEW.calculated_volume_cum := NULL;
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_parcels_vertical_3d_metrics
    BEFORE INSERT OR UPDATE OF geometry_3d ON parcels_vertical_3d
    FOR EACH ROW EXECUTE FUNCTION fn_compute_geometry_metrics();


-- ----------------------------------------------------------------------------
-- Topological validation (spec section 2.B.2): zero volumetric overlap
-- between neighbouring units on the SAME FLOOR of the SAME base parcel.
-- Two-step check on purpose: ST_3DIntersects is a cheap bounding-box-
-- accelerated pre-filter (units that merely share a party wall WILL
-- register as intersecting — that's fine and expected); only a positive
-- ST_Volume(ST_MakeSolid(ST_3DIntersection(...))) above a small
-- floating-point tolerance is an actual conflict.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_validate_topology()
RETURNS TRIGGER AS $$
DECLARE
    conflict RECORD;
    overlap_vol DOUBLE PRECISION;
BEGIN
    FOR conflict IN
        SELECT v.id, v.ulpin_3d, v.geometry_3d
        FROM parcels_vertical_3d v
        WHERE v.base_ulpin = NEW.base_ulpin
          AND v.floor_index = NEW.floor_index
          AND v.id <> COALESCE(NEW.id, -1)
          AND ST_3DIntersects(v.geometry_3d, NEW.geometry_3d)
    LOOP
        BEGIN
            overlap_vol := ST_Volume(ST_MakeSolid(
                ST_3DIntersection(conflict.geometry_3d, NEW.geometry_3d)));
        EXCEPTION WHEN OTHERS THEN
            overlap_vol := 0; -- non-solid intersection (e.g. a shared face) -> no volumetric conflict
        END;

        IF overlap_vol > 0.01 THEN -- 1e-2 m3 tolerance for shared-face floating-point noise
            RAISE EXCEPTION
                'Topology violation: unit % overlaps existing unit % by %.4f m3 on floor % of parcel %',
                NEW.ulpin_3d, conflict.ulpin_3d, overlap_vol, NEW.floor_index, NEW.base_ulpin;
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_parcels_vertical_3d_topology
    BEFORE INSERT OR UPDATE OF geometry_3d, floor_index, base_ulpin ON parcels_vertical_3d
    FOR EACH ROW EXECUTE FUNCTION fn_validate_topology();


-- ----------------------------------------------------------------------------
-- Lifecycle state-machine enforcement (spec section 2.B.1)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_validate_lifecycle_transition()
RETURNS TRIGGER AS $$
DECLARE
    prev_status VARCHAR(20);
    allowed TEXT[];
BEGIN
    SELECT status INTO prev_status
    FROM cadastral_lifecycle
    WHERE ulpin_3d = NEW.ulpin_3d
    ORDER BY effective_from DESC, id DESC
    LIMIT 1;

    IF prev_status IS NULL THEN
        IF NEW.status NOT IN ('PROPOSED','UNDER_CONSTRUCTION') THEN
            RAISE EXCEPTION
                'First lifecycle event for % must be PROPOSED or UNDER_CONSTRUCTION, got %',
                NEW.ulpin_3d, NEW.status;
        END IF;
        NEW.previous_status := NULL;
        RETURN NEW;
    END IF;

    allowed := CASE prev_status
        WHEN 'PROPOSED' THEN ARRAY['UNDER_CONSTRUCTION','DISPUTED']
        WHEN 'UNDER_CONSTRUCTION' THEN ARRAY['OC_ISSUED_ACTIVE','DISPUTED','DEMOLISHED']
        WHEN 'OC_ISSUED_ACTIVE' THEN ARRAY['DISPUTED','DEMOLISHED']
        WHEN 'DISPUTED' THEN ARRAY['PROPOSED','UNDER_CONSTRUCTION','OC_ISSUED_ACTIVE','DEMOLISHED']
        WHEN 'DEMOLISHED' THEN ARRAY[]::TEXT[]
        ELSE ARRAY[]::TEXT[]
    END;

    IF NOT (NEW.status = ANY(allowed)) THEN
        RAISE EXCEPTION 'Invalid cadastral lifecycle transition for %: % -> %',
            NEW.ulpin_3d, prev_status, NEW.status;
    END IF;

    NEW.previous_status := prev_status;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cadastral_lifecycle_transition
    BEFORE INSERT ON cadastral_lifecycle
    FOR EACH ROW EXECUTE FUNCTION fn_validate_lifecycle_transition();


-- ============================================================================
-- INDEXES
-- ============================================================================
CREATE INDEX idx_parcels_base_2d_boundary ON parcels_base_2d USING GIST (boundary_2d);
CREATE INDEX idx_parcels_base_2d_cors_pt ON parcels_base_2d USING GIST (cors_benchmark_point);
CREATE INDEX idx_parcels_vertical_3d_geom ON parcels_vertical_3d USING GIST (geometry_3d);
CREATE INDEX idx_parcels_vertical_3d_base_floor ON parcels_vertical_3d (base_ulpin, floor_index);
CREATE INDEX idx_cadastral_lifecycle_ulpin ON cadastral_lifecycle (ulpin_3d, effective_from DESC);
CREATE INDEX idx_property_tax_registry_ulpin ON property_tax_registry (ulpin_3d, assessment_year DESC);
CREATE INDEX idx_encumbrance_registry_active ON encumbrance_registry (ulpin_3d) WHERE is_active;
CREATE INDEX idx_owner_registry_current ON owner_registry (ulpin_3d) WHERE is_current;
CREATE INDEX idx_point_cloud_references_parcel ON point_cloud_references (base_ulpin);


-- ============================================================================
-- RBAC (spec section 1.7 — DPDP Act 2023 field-tiering)
-- Two DB roles map onto the API's two auth tiers. Phase 3's FastAPI layer
-- authenticates a caller and either SETs the matching DB role for that
-- connection/transaction, or (more commonly in a pooled-connection setup)
-- enforces the same tiering in application code using these views as the
-- source of truth for "what the public tier is allowed to see."
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'role_public_citizen') THEN
        CREATE ROLE role_public_citizen NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'role_authenticated_registrar') THEN
        CREATE ROLE role_authenticated_registrar NOLOGIN;
    END IF;
END $$;

-- Public/Citizen tier: 3D volume + geometry, ULPIN, RERA status, tax
-- assessment STATUS ONLY (no amounts, no owner, no lien detail).
CREATE OR REPLACE VIEW v_public_property AS
SELECT
    v.ulpin_3d,
    v.base_ulpin,
    v.vertical_type,
    v.floor_index,
    v.geometry_3d,
    v.carpet_area_sqm,
    v.calculated_volume_cum,
    cl.status AS cadastral_status,
    COALESCE((
        SELECT bool_or(e.is_active)
        FROM encumbrance_registry e
        WHERE e.ulpin_3d = v.ulpin_3d AND e.encumbrance_type = 'RERA_REGISTRATION'
    ), FALSE) AS rera_registered,
    (
        SELECT pt.payment_status
        FROM property_tax_registry pt
        WHERE pt.ulpin_3d = v.ulpin_3d
        ORDER BY pt.assessment_year DESC
        LIMIT 1
    ) AS latest_tax_payment_status
FROM parcels_vertical_3d v
LEFT JOIN LATERAL (
    SELECT status
    FROM cadastral_lifecycle
    WHERE ulpin_3d = v.ulpin_3d
    ORDER BY effective_from DESC, id DESC
    LIMIT 1
) cl ON TRUE;

COMMENT ON VIEW v_public_property IS
  'Public/Citizen RBAC tier. Deliberately does not select from '
  'owner_registry or expose encumbrance amounts/lender detail.';

GRANT SELECT ON v_public_property TO role_public_citizen;

GRANT SELECT, INSERT, UPDATE ON
    parcels_base_2d, parcels_vertical_3d, cadastral_lifecycle,
    property_tax_registry, encumbrance_registry, owner_registry,
    point_cloud_references
    TO role_authenticated_registrar;


-- Convenience view: current status per unit (latest lifecycle row).
CREATE OR REPLACE VIEW v_current_cadastral_status AS
SELECT DISTINCT ON (ulpin_3d)
    ulpin_3d, status, previous_status, effective_from, trigger_reference_type, trigger_reference_id
FROM cadastral_lifecycle
ORDER BY ulpin_3d, effective_from DESC, id DESC;

GRANT SELECT ON v_current_cadastral_status TO role_authenticated_registrar;


-- Reconciliation view: parcels whose registered units' UDS shares don't
-- sum to 100% yet. Not a hard trigger, because UDS is typically only
-- finalised once every unit in a phase/wing is registered — a blocking
-- constraint would make incremental registration impossible.
CREATE OR REPLACE VIEW v_uds_reconciliation AS
SELECT
    base_ulpin,
    COUNT(*) AS unit_count,
    SUM(uds_percentage) AS total_uds_percentage
FROM parcels_vertical_3d
WHERE uds_percentage IS NOT NULL
GROUP BY base_ulpin
HAVING SUM(uds_percentage) <> 100.0;

GRANT SELECT ON v_uds_reconciliation TO role_authenticated_registrar;


-- ============================================================================
-- USAGE EXAMPLES — the six PostGIS 3D primitives named in the brief
-- (all commented out: they reference example ULPINs, run them with real
-- values once data is loaded)
-- ============================================================================

-- ST_ZMin / ST_ZMax — elevation-range sanity check for a unit's raw geometry
-- SELECT ulpin_3d, ST_ZMin(geometry_3d) AS z_min, ST_ZMax(geometry_3d) AS z_max
-- FROM parcels_vertical_3d WHERE ulpin_3d = '110000IN0GJ0AMD088421F004U402';

-- ST_3DArea — surface area of the (pre-solid) PolyhedralSurface envelope
-- (returns 0 once the same geometry has been passed through ST_MakeSolid)
-- SELECT ulpin_3d, ST_3DArea(geometry_3d) AS envelope_area_sqm
-- FROM parcels_vertical_3d WHERE ulpin_3d = '110000IN0GJ0AMD088421F004U402';

-- ST_Volume — the brief calls this ST_3DVolume (see the naming-correction
-- note at the top of this file); requires ST_MakeSolid() on a closed surface
-- SELECT ulpin_3d, ST_Volume(ST_MakeSolid(geometry_3d)) AS volume_cum
-- FROM parcels_vertical_3d WHERE ulpin_3d = '110000IN0GJ0AMD088421F004U402';

-- ST_3DIntersects — cheap boolean pre-filter: "do these two units share
-- any 3D space at all" (this is exactly what fn_validate_topology uses
-- before spending time on a real ST_3DIntersection)
-- SELECT a.ulpin_3d, b.ulpin_3d
-- FROM parcels_vertical_3d a JOIN parcels_vertical_3d b ON a.id < b.id
-- WHERE a.base_ulpin = b.base_ulpin
--   AND ST_3DIntersects(a.geometry_3d, b.geometry_3d);

-- ST_3DIntersection — the actual overlap solid between two units (this is
-- what gets fed into ST_Volume/ST_MakeSolid to get the conflict's m3)
-- SELECT ST_Volume(ST_MakeSolid(ST_3DIntersection(a.geometry_3d, b.geometry_3d))) AS overlap_cum
-- FROM parcels_vertical_3d a, parcels_vertical_3d b
-- WHERE a.ulpin_3d = '...' AND b.ulpin_3d = '...';

-- Deviation detector preview (Phase 2 will do this properly against a
-- LiDAR as-built solid, but the primitive is the same one used above):
-- unauthorised volume = as-built solid MINUS sanctioned solid.
-- SELECT ST_Volume(ST_MakeSolid(ST_3DDifference(as_built_solid, sanctioned_solid))) AS unauthorised_cum;
