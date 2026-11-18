-- =============================================================================
-- 006_analysis.sql
-- Analysis outputs: scenarios, runs, suitability scores, proposed sites
--
-- This schema captures everything the suitability engine produces.
-- The design principle here is immutability: analysis outputs are never
-- updated in place. If a planner re-runs an analysis, a new run is created.
-- Old runs are retained for comparison and audit.
--
-- Hierarchy:
--   expansion_scenarios   — a named planning initiative (e.g. "2026 North Expansion")
--       └── analysis_runs — one execution of the suitability engine
--               └── suitability_scores  — per-parcel scores from that run
--   proposed_sites        — parcels a planner has approved from one or more runs
-- =============================================================================

-- -----------------------------------------------------------------------------
-- expansion_scenarios
-- A scenario is a named planning initiative that groups multiple analysis
-- runs together. Planners create scenarios to compare options.
-- e.g. "North Bronx Hospital Expansion 2026" might contain 5 runs with
-- different weight configurations to explore tradeoffs.
-- -----------------------------------------------------------------------------
CREATE TABLE analysis.expansion_scenarios (
    scenario_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),

    scenario_name    TEXT NOT NULL,
    description      TEXT,
    facility_type    TEXT NOT NULL,
    -- 'hospital' | 'school' | 'waste_treatment' | 'factory' | 'restaurant_zone' |
    -- 'shopping_mall' | 'office_park' | 'film_city' | 'amusement_park' |
    -- 'hiking_trail' | 'hospital'

    status           TEXT NOT NULL DEFAULT 'active',
    -- 'active' | 'archived' | 'approved' | 'rejected'

    -- Ownership
    created_by       UUID NOT NULL,   -- FK to auth.users (added when auth layer built)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,

    -- Planning context
    planning_horizon_year  SMALLINT,   -- e.g. 2030
    budget_estimate_usd    NUMERIC(16, 2),
    notes                  TEXT,

    CONSTRAINT scenarios_name_city_unique UNIQUE (city_id, scenario_name)
);

CREATE INDEX idx_scenarios_city         ON analysis.expansion_scenarios(city_id);
CREATE INDEX idx_scenarios_facility     ON analysis.expansion_scenarios(city_id, facility_type);
CREATE INDEX idx_scenarios_status       ON analysis.expansion_scenarios(city_id, status);

-- -----------------------------------------------------------------------------
-- analysis_runs
-- One row per execution of the suitability engine.
-- Captures everything needed to reproduce the run: weights, projection used,
-- data versions, run parameters.
-- -----------------------------------------------------------------------------
CREATE TABLE analysis.analysis_runs (
    run_id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scenario_id          UUID NOT NULL REFERENCES analysis.expansion_scenarios(scenario_id),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- Run identity
    run_name             TEXT,         -- optional label e.g. "high population weight"
    facility_type        TEXT NOT NULL,

    -- Execution status
    status               TEXT NOT NULL DEFAULT 'pending',
    -- 'pending' | 'running' | 'completed' | 'failed'
    started_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    duration_seconds     NUMERIC(10, 2),
    error_message        TEXT,

    -- Input counts (for audit trail)
    n_input_parcels      INTEGER,
    n_after_hard_filter  INTEGER,
    n_candidates_scored  INTEGER,

    -- Weights used (stored as JSONB — the exact weights dict from SuitabilityConfig)
    -- e.g. {"health_demand": 0.25, "gravity_catchment": 0.20, ...}
    weights_used         JSONB NOT NULL DEFAULT '{}',

    -- Hard constraint parameters used
    hard_constraints     JSONB NOT NULL DEFAULT '{}',
    -- e.g. {"min_area_m2": 10000, "flood_zone_codes": ["A","AE"], ...}

    -- Projection strategy used (FK + denormalised key fields for quick audit)
    projection_audit_id  UUID REFERENCES spatial.projection_audit(projection_audit_id),
    crs_distance_name    TEXT,
    crs_distance_family  TEXT,

    -- Data versions used (so we can reproduce the exact dataset)
    parcel_dataset_version  TEXT,
    health_grid_cdc_year    SMALLINT,
    flood_events_cutoff_date DATE,

    -- Who triggered the run
    triggered_by         UUID,         -- FK to auth.users
    triggered_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_runs_scenario    ON analysis.analysis_runs(scenario_id);
CREATE INDEX idx_runs_city        ON analysis.analysis_runs(city_id);
CREATE INDEX idx_runs_status      ON analysis.analysis_runs(city_id, status);
CREATE INDEX idx_runs_facility    ON analysis.analysis_runs(city_id, facility_type);

COMMENT ON TABLE analysis.analysis_runs IS
    'One row per engine execution. Immutable once completed. '
    'weights_used and hard_constraints JSONB fields capture exact parameters '
    'so any run can be reproduced. projection_audit_id links to CRS details.';

-- -----------------------------------------------------------------------------
-- suitability_scores
-- Per-parcel scores produced by an analysis run.
-- This is the largest table in the system — potentially millions of rows
-- across all cities and runs. Partitioned by city_id for query performance.
-- -----------------------------------------------------------------------------
CREATE TABLE analysis.suitability_scores (
    score_id         UUID DEFAULT uuid_generate_v4(),
    run_id           UUID NOT NULL REFERENCES analysis.analysis_runs(run_id),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),
    parcel_id        UUID NOT NULL REFERENCES spatial.land_parcels(parcel_id),

    -- Final composite score and rank within this run
    final_score      NUMERIC(8, 6) NOT NULL CHECK (final_score BETWEEN 0 AND 1),
    rank             INTEGER NOT NULL,

    -- Individual criterion scores (all NUMERIC 0–1)
    -- Hospital-specific scores — NULL for non-hospital runs
    score_health_demand       NUMERIC(8, 6),
    score_gravity_catchment   NUMERIC(8, 6),
    score_access_resilience   NUMERIC(8, 6),

    -- Common scores across all facility types
    score_road_access         NUMERIC(8, 6),
    score_emergency_access    NUMERIC(8, 6),
    score_flat_terrain        NUMERIC(8, 6),
    score_utility_coverage    NUMERIC(8, 6),
    score_land_cost_proxy     NUMERIC(8, 6),
    score_public_transit      NUMERIC(8, 6),

    -- Flexible JSONB column for facility-type-specific scores not in fixed columns
    -- e.g. for factories: score_wind_direction, score_rail_proximity
    -- e.g. for schools: score_child_population, score_safe_routes
    additional_scores        JSONB DEFAULT '{}',

    -- Hard constraint elimination flags (TRUE = parcel eliminated by this constraint)
    -- Stored here so planners can understand why a parcel was filtered out
    eliminated_by_area       BOOLEAN DEFAULT FALSE,
    eliminated_by_zoning     BOOLEAN DEFAULT FALSE,
    eliminated_by_flood      BOOLEAN DEFAULT FALSE,
    eliminated_by_industrial BOOLEAN DEFAULT FALSE,
    eliminated_by_road       BOOLEAN DEFAULT FALSE,
    was_eliminated           BOOLEAN GENERATED ALWAYS AS (
        eliminated_by_area OR eliminated_by_zoning OR eliminated_by_flood OR
        eliminated_by_industrial OR eliminated_by_road
    ) STORED,

    scored_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (score_id, city_id)   -- composite PK for partitioning

) PARTITION BY LIST (city_id);

-- Note: partition tables created per-city at onboarding time:
-- CREATE TABLE analysis.suitability_scores_nyc
--     PARTITION OF analysis.suitability_scores
--     FOR VALUES IN ('<nyc_city_id>');

CREATE INDEX idx_scores_run       ON analysis.suitability_scores(run_id);
CREATE INDEX idx_scores_parcel    ON analysis.suitability_scores(parcel_id);
CREATE INDEX idx_scores_rank      ON analysis.suitability_scores(run_id, rank);
CREATE INDEX idx_scores_top       ON analysis.suitability_scores(run_id, final_score DESC)
    WHERE was_eliminated = FALSE;

COMMENT ON TABLE analysis.suitability_scores IS
    'Per-parcel scores from every analysis run. Partitioned by city_id. '
    'Immutable once written — re-runs produce new rows. '
    'additional_scores JSONB holds facility-type-specific criteria not in fixed columns. '
    'elimination flag columns allow planners to explore why parcels were filtered.';

-- -----------------------------------------------------------------------------
-- proposed_sites
-- Parcels that a planner has formally selected as candidates for development.
-- These are promoted from suitability_scores by human decision — the engine
-- suggests, the planner decides.
-- -----------------------------------------------------------------------------
CREATE TABLE analysis.proposed_sites (
    site_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scenario_id      UUID NOT NULL REFERENCES analysis.expansion_scenarios(scenario_id),
    run_id           UUID NOT NULL REFERENCES analysis.analysis_runs(run_id),
    parcel_id        UUID NOT NULL REFERENCES spatial.land_parcels(parcel_id),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),

    facility_type    TEXT NOT NULL,
    site_name        TEXT,           -- planner-assigned label

    -- Decision tracking
    status           TEXT NOT NULL DEFAULT 'candidate',
    -- 'candidate' | 'shortlisted' | 'approved' | 'rejected' | 'deferred'

    -- The score that led to this proposal
    final_score      NUMERIC(8, 6),
    rank_in_run      INTEGER,

    -- Planner annotations
    planner_notes    TEXT,
    rejection_reason TEXT,

    -- Approval workflow
    proposed_by      UUID,           -- FK to auth.users
    proposed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by      UUID,
    reviewed_at      TIMESTAMPTZ,
    approved_by      UUID,
    approved_at      TIMESTAMPTZ,

    -- Planning details added after selection
    estimated_cost_usd   NUMERIC(16, 2),
    estimated_capacity   INTEGER,    -- beds, students, MW, etc. — facility-dependent
    target_open_year     SMALLINT,

    CONSTRAINT proposed_sites_unique UNIQUE (scenario_id, parcel_id)
);

CREATE INDEX idx_proposed_sites_scenario  ON analysis.proposed_sites(scenario_id);
CREATE INDEX idx_proposed_sites_city      ON analysis.proposed_sites(city_id);
CREATE INDEX idx_proposed_sites_status    ON analysis.proposed_sites(scenario_id, status);
CREATE INDEX idx_proposed_sites_parcel    ON analysis.proposed_sites(parcel_id);

COMMENT ON TABLE analysis.proposed_sites IS
    'Planner-selected candidate sites promoted from suitability_scores. '
    'The engine suggests; the planner decides. '
    'Full decision trail: proposed_by → reviewed_by → approved_by with timestamps.';

-- -----------------------------------------------------------------------------
-- scenario_comparisons
-- Records formal comparisons between two scenarios.
-- Planners often run the same facility type with different weight configurations
-- and need a structured way to record their comparison rationale.
-- -----------------------------------------------------------------------------
CREATE TABLE analysis.scenario_comparisons (
    comparison_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),
    scenario_a_id    UUID NOT NULL REFERENCES analysis.expansion_scenarios(scenario_id),
    scenario_b_id    UUID NOT NULL REFERENCES analysis.expansion_scenarios(scenario_id),

    comparison_notes TEXT,
    preferred_scenario_id UUID REFERENCES analysis.expansion_scenarios(scenario_id),
    preference_rationale  TEXT,

    created_by       UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT comparison_different_scenarios CHECK (scenario_a_id <> scenario_b_id)
);
