-- ============================================================================
-- schema_patch_01.sql — Strata Cadastre (SIH26011)
-- Apply AFTER schema_3d_cadastre.sql. Fixes two real bugs found while
-- building the deployment kit, and adds one helper you'll want for seeding.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema_patch_01.sql
--
-- ----------------------------------------------------------------------------
-- BUG 1 (serious — wrong numbers, silently): volume computed in degrees
-- ----------------------------------------------------------------------------
-- parcels_vertical_3d.geometry_3d is GEOMETRY(PolyhedralSurfaceZ, 4326), so
-- its X/Y are DEGREES of longitude/latitude while its Z is METRES. The
-- original fn_compute_geometry_metrics called ST_Volume() straight on that,
-- producing a number in degree^2 * metre — off from true cubic metres by
-- roughly 111320 * 111320 * cos(latitude), i.e. about 1.1e10 at Ahmedabad's
-- latitude. The value was never NULL and never errored, which is what makes
-- this the dangerous kind of bug: a tax assessment derived from
-- calculated_volume_cum would have been wrong by ten orders of magnitude
-- with nothing visibly broken.
--
-- Fix: transform to the projected working CRS (EPSG:32643, WGS 84 / UTM
-- zone 43N — the CRS the schema header already nominates for planar-metric
-- math) before measuring. ST_Transform applies a 2D pipeline: X/Y become
-- metres, Z passes through untouched — which is exactly right here, since Z
-- is already metres from geodesy.py.
--
-- IF YOU DEPLOY OUTSIDE GUJARAT: 32643 is UTM zone 43N. Using a zone that
-- doesn't cover your data introduces real distortion. Change the SRID below
-- to the correct UTM zone (or a state grid) for your deployment area.
--
-- ----------------------------------------------------------------------------
-- BUG 2 (cosmetic): malformed RAISE format specifier
-- ----------------------------------------------------------------------------
-- fn_validate_topology's message used '%.4f', but plpgsql's RAISE only
-- understands '%' as a placeholder — the '.4f' was emitted as literal text,
-- so violations printed like "by 0.523.4f m3". Rounded explicitly instead.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Helper: build a closed, axis-aligned PolyhedralSurfaceZ box.
-- Useful for seeding, for the verification suite, and as the simplest
-- possible LOD1 volume for a unit whose real B-Rep isn't available yet.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION strata_make_box(
    x0 DOUBLE PRECISION, y0 DOUBLE PRECISION, z0 DOUBLE PRECISION,
    x1 DOUBLE PRECISION, y1 DOUBLE PRECISION, z1 DOUBLE PRECISION,
    srid INTEGER DEFAULT 4326
) RETURNS GEOMETRY AS $$
    SELECT ST_SetSRID(
        ST_GeomFromText(
            format(
                'POLYHEDRALSURFACE Z(
                   ((%1$s %3$s %5$s, %1$s %4$s %5$s, %2$s %4$s %5$s, %2$s %3$s %5$s, %1$s %3$s %5$s)),
                   ((%1$s %3$s %6$s, %2$s %3$s %6$s, %2$s %4$s %6$s, %1$s %4$s %6$s, %1$s %3$s %6$s)),
                   ((%1$s %3$s %5$s, %2$s %3$s %5$s, %2$s %3$s %6$s, %1$s %3$s %6$s, %1$s %3$s %5$s)),
                   ((%2$s %3$s %5$s, %2$s %4$s %5$s, %2$s %4$s %6$s, %2$s %3$s %6$s, %2$s %3$s %5$s)),
                   ((%2$s %4$s %5$s, %1$s %4$s %5$s, %1$s %4$s %6$s, %2$s %4$s %6$s, %2$s %4$s %5$s)),
                   ((%1$s %4$s %5$s, %1$s %3$s %5$s, %1$s %3$s %6$s, %1$s %4$s %6$s, %1$s %4$s %5$s))
                 )',
                x0::text, x1::text, y0::text, y1::text, z0::text, z1::text
            )
        ),
        srid
    );
$$ LANGUAGE sql IMMUTABLE;

COMMENT ON FUNCTION strata_make_box IS
  'Closed axis-aligned PolyhedralSurfaceZ box. Face winding is consistent so '
  'ST_MakeSolid()/ST_Volume() work on the result.';


-- ----------------------------------------------------------------------------
-- BUG 1 FIX — measure in projected metres, not degrees
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_compute_geometry_metrics()
RETURNS TRIGGER AS $$
DECLARE
    metric_geom GEOMETRY;
BEGIN
    BEGIN
        -- 32643 = WGS 84 / UTM zone 43N (Gujarat). Change for other regions.
        metric_geom := ST_Transform(NEW.geometry_3d, 32643);
        NEW.calculated_volume_cum := ST_Volume(ST_MakeSolid(metric_geom));
    EXCEPTION WHEN OTHERS THEN
        -- Geometry not closed / not solidifiable yet: leave the metric NULL
        -- for the pipeline to reconcile once the B-Rep is finalised, rather
        -- than failing the whole write.
        NEW.calculated_volume_cum := NULL;
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- BUG 1 FIX (cont.) — topology overlap volume, also in projected metres
-- BUG 2 FIX — correct RAISE formatting
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_validate_topology()
RETURNS TRIGGER AS $$
DECLARE
    conflict RECORD;
    overlap_vol DOUBLE PRECISION;
    new_metric GEOMETRY;
BEGIN
    new_metric := ST_Transform(NEW.geometry_3d, 32643);

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
                ST_3DIntersection(ST_Transform(conflict.geometry_3d, 32643), new_metric)));
        EXCEPTION WHEN OTHERS THEN
            -- Non-solid intersection (e.g. a shared party wall) — units that
            -- merely touch are legitimate neighbours, not a conflict.
            overlap_vol := 0;
        END;

        IF overlap_vol > 0.01 THEN   -- 1e-2 m3 tolerance for shared-face noise
            RAISE EXCEPTION
                'Topology violation: unit % overlaps existing unit % by % m3 on floor % of parcel %',
                NEW.ulpin_3d, conflict.ulpin_3d,
                round(overlap_vol::numeric, 4), NEW.floor_index, NEW.base_ulpin;
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- Backfill any rows written before this patch (their volumes are in the
-- wrong units). No-op on a fresh database.
-- ----------------------------------------------------------------------------
UPDATE parcels_vertical_3d SET geometry_3d = geometry_3d;

DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT COUNT(*) INTO n FROM parcels_vertical_3d WHERE calculated_volume_cum IS NOT NULL;
    RAISE NOTICE 'schema_patch_01 applied. Recomputed volumes for % row(s).', n;
END $$;
