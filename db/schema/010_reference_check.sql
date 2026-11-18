-- =============================================================================
-- 010_reference_check.sql
-- Spatial index reference check: index scan vs sequential full scan comparison
--
-- Purpose
-- -------
-- A spatial GIST index can silently produce incorrect results if:
--   - The index was built on data that was subsequently modified without
--     triggering an index update (e.g. bulk COPY with maintenance_work_mem
--     exhausted, partial index build failure)
--   - Index pages became corrupt (storage fault, PostgreSQL bug)
--   - The index statistics are stale causing the planner to use it incorrectly
--
-- These failures are silent — queries return results, but wrong ones.
-- For a city planning system where incorrect spatial results could lead to
-- a hospital being sited in a flood zone, this is unacceptable.
--
-- The reference check
-- --------------------
-- For a given city and bounding box (the reference window):
--   1. Run the query WITH the spatial index (normal path)
--   2. Run the same query WITH the index DISABLED (sequential scan)
--   3. Compare result sets:
--        - Same parcel IDs in both → index is consistent
--        - Index scan returns rows not in seq scan → index has phantom entries
--        - Seq scan returns rows not in index scan → index is missing entries
--   4. Log the outcome to spatial.index_check_log
--   5. Raise a WARNING (not an error) if inconsistency detected — the
--      application can still run but the anomaly is recorded
--
-- The reference window
-- ---------------------
-- We use a 2km × 2km box centred on the city centroid as the reference window.
-- This is small enough that a full sequential scan completes quickly (~seconds)
-- but large enough to contain a meaningful sample of parcels (typically 500-2000).
-- The result is compared by parcel_id set — geometry values are not compared
-- because the GIST index returns the same rows as seq scan for valid indexes.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Index check log — records every reference check run
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spatial.index_check_log (
    check_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id             UUID NOT NULL REFERENCES spatial.cities(city_id),
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    check_type          TEXT NOT NULL DEFAULT 'parcel_spatial_index',

    -- Reference window used for this check
    ref_bbox            GEOMETRY(POLYGON, 4326),
    ref_bbox_wkt        TEXT,       -- human-readable WKT for logs

    -- Result counts
    n_index_scan        INTEGER,    -- rows returned by index scan path
    n_seq_scan          INTEGER,    -- rows returned by sequential scan path
    n_only_in_index     INTEGER,    -- phantom rows (in index, not in seq scan)
    n_only_in_seq_scan  INTEGER,    -- missing rows (in seq scan, not in index)
    n_matched           INTEGER,    -- rows in both (the good count)

    -- Outcome
    passed              BOOLEAN NOT NULL,
    -- TRUE  = index and seq scan agree completely
    -- FALSE = discrepancy detected

    discrepancy_detail  JSONB,
    -- When passed=FALSE: contains sample of mismatched parcel_ids for diagnosis
    -- e.g. {"phantom_sample": ["uuid1","uuid2"], "missing_sample": ["uuid3"]}

    -- Performance
    index_scan_ms       NUMERIC(10,2),
    seq_scan_ms         NUMERIC(10,2),
    speedup_factor      NUMERIC(8,2)  -- index_scan_ms / seq_scan_ms
);

CREATE INDEX idx_index_check_city ON spatial.index_check_log(city_id, checked_at DESC);
CREATE INDEX idx_index_check_fail ON spatial.index_check_log(city_id)
    WHERE passed = FALSE;

COMMENT ON TABLE spatial.index_check_log IS
    'Records every spatial index reference check. '
    'passed=FALSE rows indicate index/data inconsistency and trigger warnings. '
    'Retained indefinitely — historical pass/fail pattern reveals degrading indexes.';

-- -----------------------------------------------------------------------------
-- Core reference check function
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION spatial.run_index_reference_check(
    p_city_id       UUID,
    p_ref_bbox      GEOMETRY DEFAULT NULL
    -- NULL = auto-generate 2km x 2km box around city centroid
)
RETURNS spatial.index_check_log
LANGUAGE plpgsql AS $$
DECLARE
    v_ref_bbox          GEOMETRY;
    v_city_centroid     GEOMETRY;
    v_index_ids         UUID[];
    v_seq_ids           UUID[];
    v_only_index        UUID[];
    v_only_seq          UUID[];
    v_matched           UUID[];
    v_index_start       TIMESTAMPTZ;
    v_seq_start         TIMESTAMPTZ;
    v_index_ms          NUMERIC;
    v_seq_ms            NUMERIC;
    v_passed            BOOLEAN;
    v_detail            JSONB;
    v_result            spatial.index_check_log;
BEGIN

    -- -------------------------------------------------------------------------
    -- Build reference bbox if not supplied
    -- Use a 2km radius circle around the city centroid, converted to bbox
    -- -------------------------------------------------------------------------
    IF p_ref_bbox IS NULL THEN
        SELECT ST_Centroid(geometry)
        INTO v_city_centroid
        FROM spatial.city_boundaries
        WHERE city_id = p_city_id
          AND valid_to IS NULL
          AND boundary_type = 'municipal'
        LIMIT 1;

        IF v_city_centroid IS NULL THEN
            RAISE EXCEPTION 'No active municipal boundary found for city_id %', p_city_id;
        END IF;

        -- 2km buffer in geography (metres), cast back to geometry
        v_ref_bbox := ST_Envelope(
            ST_Buffer(v_city_centroid::GEOGRAPHY, 2000)::GEOMETRY
        );
    ELSE
        v_ref_bbox := p_ref_bbox;
    END IF;

    -- -------------------------------------------------------------------------
    -- Run 1: Index scan path (normal — uses GIST index)
    -- -------------------------------------------------------------------------
    v_index_start := clock_timestamp();

    SELECT ARRAY_AGG(parcel_id ORDER BY parcel_id)
    INTO v_index_ids
    FROM spatial.land_parcels
    WHERE city_id = p_city_id
      AND valid_to IS NULL
      AND ST_Intersects(geometry, v_ref_bbox);
      -- PostgreSQL will use the GIST index here via normal query planning

    v_index_ms := EXTRACT(EPOCH FROM (clock_timestamp() - v_index_start)) * 1000;

    -- -------------------------------------------------------------------------
    -- Run 2: Sequential scan (force disable index scan for this transaction)
    -- We use a CTE with a SET LOCAL to avoid affecting other sessions
    -- -------------------------------------------------------------------------
    v_seq_start := clock_timestamp();

    -- Temporarily disable index scans for the sequential scan query
    -- This is session-local and resets at end of transaction
    SET LOCAL enable_indexscan = OFF;
    SET LOCAL enable_bitmapscan = OFF;

    SELECT ARRAY_AGG(parcel_id ORDER BY parcel_id)
    INTO v_seq_ids
    FROM spatial.land_parcels
    WHERE city_id = p_city_id
      AND valid_to IS NULL
      AND ST_Intersects(geometry, v_ref_bbox);

    -- Restore index scan settings
    SET LOCAL enable_indexscan = ON;
    SET LOCAL enable_bitmapscan = ON;

    v_seq_ms := EXTRACT(EPOCH FROM (clock_timestamp() - v_seq_start)) * 1000;

    -- -------------------------------------------------------------------------
    -- Compare result sets
    -- -------------------------------------------------------------------------

    -- Rows only in index scan (phantom entries — index has stale data)
    SELECT ARRAY_AGG(id)
    INTO v_only_index
    FROM UNNEST(COALESCE(v_index_ids, ARRAY[]::UUID[])) AS id
    WHERE id <> ALL(COALESCE(v_seq_ids, ARRAY[]::UUID[]));

    -- Rows only in seq scan (missing from index — index is incomplete)
    SELECT ARRAY_AGG(id)
    INTO v_only_seq
    FROM UNNEST(COALESCE(v_seq_ids, ARRAY[]::UUID[])) AS id
    WHERE id <> ALL(COALESCE(v_index_ids, ARRAY[]::UUID[]));

    -- Matched rows
    SELECT ARRAY_AGG(id)
    INTO v_matched
    FROM UNNEST(COALESCE(v_index_ids, ARRAY[]::UUID[])) AS id
    WHERE id = ANY(COALESCE(v_seq_ids, ARRAY[]::UUID[]));

    v_passed := (COALESCE(ARRAY_LENGTH(v_only_index, 1), 0) = 0)
            AND (COALESCE(ARRAY_LENGTH(v_only_seq,   1), 0) = 0);

    -- Build discrepancy detail JSONB (only populated when check fails)
    IF NOT v_passed THEN
        v_detail := jsonb_build_object(
            'phantom_count',   COALESCE(ARRAY_LENGTH(v_only_index, 1), 0),
            'missing_count',   COALESCE(ARRAY_LENGTH(v_only_seq,   1), 0),
            -- Sample up to 10 mismatched IDs for diagnosis without bloating the log
            'phantom_sample',  TO_JSONB(v_only_index[1:10]),
            'missing_sample',  TO_JSONB(v_only_seq[1:10]),
            'recommendation',  'Run REINDEX INDEX CONCURRENTLY on affected partition. '
                            || 'If problem persists, run VACUUM FULL on the partition.'
        );

        RAISE WARNING
            'SPATIAL INDEX CHECK FAILED for city_id=%. '
            'Phantom entries (in index, not in data): %. '
            'Missing entries (in data, not in index): %. '
            'Run REINDEX INDEX CONCURRENTLY to repair.',
            p_city_id,
            COALESCE(ARRAY_LENGTH(v_only_index, 1), 0),
            COALESCE(ARRAY_LENGTH(v_only_seq,   1), 0);
    END IF;

    -- -------------------------------------------------------------------------
    -- Write result to log
    -- -------------------------------------------------------------------------
    INSERT INTO spatial.index_check_log (
        city_id, ref_bbox, ref_bbox_wkt,
        n_index_scan, n_seq_scan,
        n_only_in_index, n_only_in_seq_scan, n_matched,
        passed, discrepancy_detail,
        index_scan_ms, seq_scan_ms, speedup_factor
    ) VALUES (
        p_city_id,
        v_ref_bbox,
        ST_AsText(v_ref_bbox),
        COALESCE(ARRAY_LENGTH(v_index_ids, 1), 0),
        COALESCE(ARRAY_LENGTH(v_seq_ids,   1), 0),
        COALESCE(ARRAY_LENGTH(v_only_index, 1), 0),
        COALESCE(ARRAY_LENGTH(v_only_seq,   1), 0),
        COALESCE(ARRAY_LENGTH(v_matched,    1), 0),
        v_passed,
        v_detail,
        v_index_ms,
        v_seq_ms,
        CASE WHEN v_index_ms > 0 THEN v_seq_ms / v_index_ms ELSE NULL END
    )
    RETURNING * INTO v_result;

    RETURN v_result;
END;
$$;

COMMENT ON FUNCTION spatial.run_index_reference_check IS
    'Compares index scan vs sequential scan results for a reference bbox. '
    'Logs outcome to spatial.index_check_log. '
    'Raises WARNING (not exception) on failure so the caller can decide '
    'whether to proceed or abort the analysis run.';
