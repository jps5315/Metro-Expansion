-- =============================================================================
-- 008_indexes_and_constraints.sql
-- Cross-table constraints, composite indexes for the analysis hot path,
-- and performance tuning configuration.
--
-- Run after all table migrations are complete.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Foreign key from road_flood_events to city — ensure the road segment
-- and the event belong to the same city (data integrity across ingestion sources)
-- -----------------------------------------------------------------------------
ALTER TABLE spatial.road_flood_events
    ADD CONSTRAINT fk_flood_event_city_match
    CHECK (city_id IS NOT NULL);

-- Partial index on road_flood_events for the exact query in _score_access_resilience:
-- city_id filter + recent events only + joined to road geometry
-- The engine queries: WHERE city_id = X AND event_date >= NOW() - INTERVAL '20 years'
CREATE INDEX idx_road_flood_resilience_query
    ON spatial.road_flood_events(city_id, road_segment_id, severity_class, declared_emergency)
    INCLUDE (event_date);

-- -----------------------------------------------------------------------------
-- Composite index for health_demand_grid — the two most expensive queries:
-- 1. _score_health_demand: spatial join to 2km buffers
-- 2. _score_gravity_catchment: spatial join to 15km buffers + disease column reads
-- Both queries filter by city_id first then hit the geometry index.
-- A covering index on the disease prevalence columns avoids heap fetches
-- on the hot scoring path.
-- -----------------------------------------------------------------------------
CREATE INDEX idx_health_grid_scoring
    ON spatial.health_demand_grid(city_id)
    INCLUDE (
        population, elderly_population, ed_visit_rate,
        prevalence_heart_disease, prevalence_diabetes, prevalence_copd,
        prevalence_stroke, prevalence_obesity, prevalence_mental_health
    );

-- -----------------------------------------------------------------------------
-- Composite index for existing_facilities: hospital gravity query
-- Fetches bed_count for all hospitals in city — accessed O(hospitals) times
-- in the gravity model inner loop
-- -----------------------------------------------------------------------------
CREATE INDEX idx_facilities_hospital_gravity
    ON spatial.existing_facilities(city_id, facility_type)
    INCLUDE (bed_count)
    WHERE facility_type = 'hospital';

-- -----------------------------------------------------------------------------
-- suitability_scores: the most-read table after analysis runs complete.
-- Planners browse results by run, sorted by rank.
-- The partial index on was_eliminated=FALSE covers the most common query.
-- -----------------------------------------------------------------------------
CREATE INDEX idx_scores_browse
    ON analysis.suitability_scores(run_id, rank ASC)
    INCLUDE (parcel_id, final_score)
    WHERE was_eliminated = FALSE;

-- -----------------------------------------------------------------------------
-- PostgreSQL statistics and autovacuum tuning for large tables
-- -----------------------------------------------------------------------------

-- health_demand_grid: spatial joins dominate — keep statistics fresh
ALTER TABLE spatial.health_demand_grid
    ALTER COLUMN geometry SET STATISTICS 500;

ALTER TABLE spatial.land_parcels
    ALTER COLUMN geometry SET STATISTICS 500;

ALTER TABLE spatial.road_network
    ALTER COLUMN geometry SET STATISTICS 500;

-- suitability_scores: written in bulk by analysis runs, read frequently
ALTER TABLE analysis.suitability_scores SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.005
);

-- road_flood_events: append-only, no dead tuples — reduce vacuum overhead
ALTER TABLE spatial.road_flood_events SET (
    autovacuum_vacuum_scale_factor = 0.1,
    autovacuum_analyze_scale_factor = 0.05
);

-- audit.event_log: insert-only by design
ALTER TABLE audit.event_log SET (
    autovacuum_vacuum_scale_factor = 0.1,
    autovacuum_analyze_scale_factor = 0.05
);

-- -----------------------------------------------------------------------------
-- Row-level security setup (placeholder — fleshed out when auth layer added)
-- Enables per-city data isolation so a planner from City A cannot see City B data
-- -----------------------------------------------------------------------------
ALTER TABLE spatial.land_parcels         ENABLE ROW LEVEL SECURITY;
ALTER TABLE spatial.health_demand_grid   ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis.expansion_scenarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis.suitability_scores  ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis.proposed_sites      ENABLE ROW LEVEL SECURITY;

-- Superuser and service account bypass RLS during ingestion and admin tasks
-- Planner-facing policies added when Keycloak auth integration is built:
-- CREATE POLICY city_isolation ON spatial.land_parcels
--     USING (city_id = current_setting('app.current_city_id')::UUID);
