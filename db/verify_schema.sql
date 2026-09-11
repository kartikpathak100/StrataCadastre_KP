-- ============================================================================
-- verify_schema.sql — Strata Cadastre (SIH26011)
-- One command that tells you whether your database can actually run this
-- system. Every check RAISEs on failure, so with ON_ERROR_STOP the script
-- exits non-zero the moment something is wrong — no silent passes.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f verify_schema.sql
--
-- Run order: schema_3d_cadastre.sql -> schema_patch_01.sql -> this file.
--
-- Everything happens inside a transaction that ROLLBACKs at the end, so this
-- is safe to run against a database with real data in it.
-- ============================================================================

\set ON_ERROR_STOP on
\timing off

BEGIN;

-- ----------------------------------------------------------------------------
-- CHECK 1 — required extensions are present and SFCGAL actually works
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    has_postgis BOOLEAN;
    has_sfcgal BOOLEAN;
    test_vol DOUBLE PRECISION;
BEGIN
    SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'postgis') INTO has_postgis;
    SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'postgis_sfcgal') INTO has_sfcgal;

    IF NOT has_postgis THEN
        RAISE EXCEPTION 'CHECK 1 FAILED: postgis extension is not installed.';
    END IF;
    IF NOT has_sfcgal THEN
        RAISE EXCEPTION
            'CHECK 1 FAILED: postgis_sfcgal is not installed. The 3D volume '
            'functions this system depends on will not work. Note that AWS RDS '
            'does not offer SFCGAL at all, and the Alpine variants of the '
            'postgis/postgis Docker image do not support it either — use a '
            'Debian-based postgis/postgis tag, Neon, or self-hosted Postgres.';
    END IF;

    -- Prove SFCGAL is genuinely functional, not merely registered: a 1x1x1
    -- metre cube must measure exactly 1 m3 once made solid.
    SELECT ST_Volume(ST_MakeSolid(strata_make_box(0, 0, 0, 1, 1, 1, 32643))) INTO test_vol;
    IF test_vol IS NULL OR abs(test_vol - 1.0) > 1e-6 THEN
        RAISE EXCEPTION 'CHECK 1 FAILED: unit cube measured % m3, expected 1.0', test_vol;
    END IF;

    RAISE NOTICE 'CHECK 1 PASS — postgis + postgis_sfcgal present, unit cube = % m3', test_vol;
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 2 — all expected tables and views exist
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    expected TEXT[] := ARRAY['parcels_base_2d','parcels_vertical_3d','cadastral_lifecycle',
                              'property_tax_registry','encumbrance_registry','owner_registry',
                              'point_cloud_references'];
    missing TEXT[];
BEGIN
    SELECT array_agg(t) INTO missing
    FROM unnest(expected) AS t
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = t
    );

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'CHECK 2 FAILED: missing tables: %', array_to_string(missing, ', ');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.views
                   WHERE table_schema = 'public' AND table_name = 'v_public_property') THEN
        RAISE EXCEPTION 'CHECK 2 FAILED: v_public_property view is missing.';
    END IF;

    RAISE NOTICE 'CHECK 2 PASS — all 7 tables and the public RBAC view exist';
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 3 — insert a parcel + unit; the volume trigger must produce a
-- sane CUBIC METRE figure (this is the check that catches the 4326-degrees
-- bug that schema_patch_01.sql fixes)
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    vol DOUBLE PRECISION;
    expected_vol DOUBLE PRECISION := 165.0 * 3.2;   -- 15 m x 11 m x 3.2 m = 528 m3
BEGIN
    INSERT INTO parcels_base_2d (
        ulpin_2d, lgd_state_code, lgd_district_code, lgd_subdistrict_code,
        total_parcel_area_sqm, boundary_2d, source_confidence_score, positional_uncertainty_cm
    ) VALUES (
        '11000088421000', '24', '0124', '024001', 3843.00,
        ST_SetSRID(ST_GeomFromText(
            'MULTIPOLYGON(((72.57130 23.02240, 72.57165 23.02240,
                            72.57165 23.02270, 72.57130 23.02270, 72.57130 23.02240)))'), 4326),
        0.940, 18.00
    );

    -- A 15 m x 11 m x 3.2 m unit, built in UTM 43N metres then transformed
    -- into the 4326 storage CRS — the same path the ingestion pipeline uses.
    INSERT INTO parcels_vertical_3d (
        ulpin_3d, base_ulpin, vertical_type, floor_index, unit_code, geometry_3d,
        orthometric_base_z_m, orthometric_top_z_m, carpet_area_sqm, uds_percentage,
        source_confidence_score, positional_uncertainty_cm
    ) VALUES (
        '24012411000088421000F0040402', '11000088421000', 'F', 4, '0402',
        ST_Transform(strata_make_box(457000, 2545000, 12.8, 457015, 2545011, 16.0, 32643), 4326),
        177.555, 180.755, 165.00, 16.6667, 0.940, 18.00
    );

    SELECT calculated_volume_cum INTO vol
    FROM parcels_vertical_3d WHERE ulpin_3d = '24012411000088421000F0040402';

    IF vol IS NULL THEN
        RAISE EXCEPTION 'CHECK 3 FAILED: calculated_volume_cum is NULL — the '
                         'metrics trigger did not fire or the geometry is not solidifiable.';
    END IF;

    -- Allow 1% for the round trip through 4326 and back.
    IF abs(vol - expected_vol) / expected_vol > 0.01 THEN
        RAISE EXCEPTION
            'CHECK 3 FAILED: volume is % but should be about % m3. If it is off by '
            'roughly 1e10, schema_patch_01.sql has not been applied and volumes are '
            'being measured in degrees, not metres.', round(vol::numeric, 4), expected_vol;
    END IF;

    RAISE NOTICE 'CHECK 3 PASS — unit volume = % m3 (expected ~% m3)',
        round(vol::numeric, 2), expected_vol;
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 4 — the topology trigger REJECTS a genuinely overlapping unit
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    rejected BOOLEAN := FALSE;
BEGIN
    BEGIN
        INSERT INTO parcels_vertical_3d (
            ulpin_3d, base_ulpin, vertical_type, floor_index, unit_code, geometry_3d,
            orthometric_base_z_m, orthometric_top_z_m, carpet_area_sqm
        ) VALUES (
            '24012411000088421000F0040403', '11000088421000', 'F', 4, '0403',
            -- Deliberately overlaps the CHECK 3 unit by 10 m of its width.
            ST_Transform(strata_make_box(457005, 2545000, 12.8, 457020, 2545011, 16.0, 32643), 4326),
            177.555, 180.755, 165.00
        );
    EXCEPTION WHEN OTHERS THEN
        rejected := TRUE;
        RAISE NOTICE 'CHECK 4 PASS — overlap correctly rejected: %', SQLERRM;
    END;

    IF NOT rejected THEN
        RAISE EXCEPTION 'CHECK 4 FAILED: an overlapping unit was accepted. '
                         'Two people can now legally own the same cubic metres.';
    END IF;
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 5 — a NON-overlapping neighbour on the same floor is ACCEPTED
-- (a party wall is not a conflict; this is the false-positive guard)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    INSERT INTO parcels_vertical_3d (
        ulpin_3d, base_ulpin, vertical_type, floor_index, unit_code, geometry_3d,
        orthometric_base_z_m, orthometric_top_z_m, carpet_area_sqm
    ) VALUES (
        '24012411000088421000F0040404', '11000088421000', 'F', 4, '0404',
        -- Shares the x=457015 face with the CHECK 3 unit — touching, not overlapping.
        ST_Transform(strata_make_box(457015, 2545000, 12.8, 457030, 2545011, 16.0, 32643), 4326),
        177.555, 180.755, 165.00
    );
    RAISE NOTICE 'CHECK 5 PASS — adjacent unit sharing a party wall was accepted';
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'CHECK 5 FAILED: a legitimate adjacent unit was rejected as a '
                     'topology conflict (false positive): %', SQLERRM;
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 6 — lifecycle state machine enforces legal transitions
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    rejected BOOLEAN := FALSE;
BEGIN
    -- Illegal: a unit's first event cannot be OC_ISSUED_ACTIVE.
    BEGIN
        INSERT INTO cadastral_lifecycle (ulpin_3d, status)
        VALUES ('24012411000088421000F0040402', 'OC_ISSUED_ACTIVE');
    EXCEPTION WHEN OTHERS THEN
        rejected := TRUE;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'CHECK 6 FAILED: illegal first lifecycle state was accepted.';
    END IF;

    -- Legal path: PROPOSED -> UNDER_CONSTRUCTION -> OC_ISSUED_ACTIVE
    INSERT INTO cadastral_lifecycle (ulpin_3d, status)
        VALUES ('24012411000088421000F0040402', 'PROPOSED');
    INSERT INTO cadastral_lifecycle (ulpin_3d, status)
        VALUES ('24012411000088421000F0040402', 'UNDER_CONSTRUCTION');
    INSERT INTO cadastral_lifecycle (ulpin_3d, status, trigger_reference_type, trigger_reference_id)
        VALUES ('24012411000088421000F0040402', 'OC_ISSUED_ACTIVE',
                'MUNICIPAL_OC_CERTIFICATE', 'AMC/OC/2026/11482');

    -- Illegal: nothing comes back from DEMOLISHED.
    INSERT INTO cadastral_lifecycle (ulpin_3d, status)
        VALUES ('24012411000088421000F0040402', 'DEMOLISHED');
    rejected := FALSE;
    BEGIN
        INSERT INTO cadastral_lifecycle (ulpin_3d, status)
            VALUES ('24012411000088421000F0040402', 'OC_ISSUED_ACTIVE');
    EXCEPTION WHEN OTHERS THEN
        rejected := TRUE;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'CHECK 6 FAILED: a DEMOLISHED unit was resurrected.';
    END IF;

    RAISE NOTICE 'CHECK 6 PASS — lifecycle transitions enforced in both directions';
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 7 — the ULPIN format constraint rejects malformed identifiers
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    rejected BOOLEAN := FALSE;
BEGIN
    BEGIN
        INSERT INTO parcels_vertical_3d (
            ulpin_3d, base_ulpin, vertical_type, floor_index, unit_code, geometry_3d,
            orthometric_base_z_m, orthometric_top_z_m, carpet_area_sqm
        ) VALUES (
            'NOT-A-VALID-ULPIN-STRING-123', '11000088421000', 'F', 9, '0900',
            ST_Transform(strata_make_box(457100, 2545100, 28.8, 457115, 2545111, 32.0, 32643), 4326),
            193.5, 196.7, 165.00
        );
    EXCEPTION WHEN OTHERS THEN
        rejected := TRUE;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'CHECK 7 FAILED: a malformed 3D-ULPIN was accepted.';
    END IF;
    RAISE NOTICE 'CHECK 7 PASS — malformed 3D-ULPIN rejected by chk_ulpin_3d_format';
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 8 — DPDP tiering: the public view must not expose owner data
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    leaked TEXT[];
    row_count INTEGER;
BEGIN
    INSERT INTO owner_registry (ulpin_3d, owner_name, kyc_verification_status, sale_deed_hash)
    VALUES ('24012411000088421000F0040402', 'Sanjay Desai', 'VERIFIED', '0x8f2ac471');

    SELECT array_agg(column_name::text) INTO leaked
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'v_public_property'
      AND column_name IN ('owner_name','kyc_reference_hash','sale_deed_hash',
                           'amount_secured','lender_or_authority');

    IF leaked IS NOT NULL THEN
        RAISE EXCEPTION 'CHECK 8 FAILED: v_public_property exposes registrar-tier '
                         'columns: %', array_to_string(leaked, ', ');
    END IF;

    SELECT COUNT(*) INTO row_count FROM v_public_property
    WHERE ulpin_3d = '24012411000088421000F0040402';
    IF row_count <> 1 THEN
        RAISE EXCEPTION 'CHECK 8 FAILED: expected 1 row from v_public_property, got %', row_count;
    END IF;

    RAISE NOTICE 'CHECK 8 PASS — public view returns the unit with no owner/KYC/lien columns';
END $$;


-- ----------------------------------------------------------------------------
-- CHECK 9 — spatial indexes exist (a city-scale cadastre without these
-- degrades to sequential scans very quickly)
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT COUNT(*) INTO n FROM pg_indexes
    WHERE schemaname = 'public'
      AND indexname IN ('idx_parcels_base_2d_boundary','idx_parcels_vertical_3d_geom');
    IF n < 2 THEN
        RAISE EXCEPTION 'CHECK 9 FAILED: expected 2 GIST spatial indexes, found %', n;
    END IF;
    RAISE NOTICE 'CHECK 9 PASS — GIST spatial indexes present';
END $$;


DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '================================================================';
    RAISE NOTICE 'ALL CHECKS PASSED — this database can run Strata Cadastre.';
    RAISE NOTICE 'Rolling back test data now; your database is unchanged.';
    RAISE NOTICE '================================================================';
END $$;

ROLLBACK;
