-- =============================================================================
-- 005_environmental.sql
-- Environmental zones and health demand data
--
-- Covers:
--   - environmental_zones: flood zones, industrial buffers, protected areas
--   - health_demand_grid: CDC PLACES disease burden + demographics
--   - demographic_grid: population distribution (kept separate from health
--     demand so demographic data can be updated independently)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- environmental_zones
-- Polygon layer of zones that constrain or affect suitability.
-- Multiple zone_type values share one table — discriminated by zone_type.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.environmental_zones (
    zone_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),

    zone_type        TEXT NOT NULL,
    -- 'flood'              — FEMA / national flood zone designation
    -- 'industrial_buffer'  — buffer around industrial/hazmat sites
    -- 'protected_area'     — national park, wildlife reserve, wetlands
    -- 'noise_contour'      — airport/highway noise exposure zones
    -- 'air_quality'        — EPA non-attainment or sensitive airshed zones
    -- 'contamination'      — Superfund / brownfield contamination zones
    -- 'coastal_setback'    — mandatory setback from coastline

    zone_code        TEXT,
    -- For flood: 'A','AE','AH','AO','VE','X' (FEMA designations)
    -- For protected: IUCN category (I-VI)
    -- For noise: Ldn dB range e.g. '65-70dB'

    zone_name        TEXT,
    description      TEXT,

    -- Risk/severity level (standardised 1-5 scale across all zone types)
    -- 5 = highest constraint / risk, 1 = minimal
    severity_level   SMALLINT CHECK (severity_level BETWEEN 1 AND 5),

    -- Flood-specific attributes (NULL for non-flood zones)
    flood_return_period_years  INTEGER,   -- 10, 50, 100, 500 year event
    base_flood_elevation_m     NUMERIC(8, 3),
    flood_zone_source          TEXT,      -- 'FEMA_FIRM' | 'LOCAL_MODEL' | 'FEMA_NFIP'

    -- Industrial buffer specific
    hazmat_class               TEXT[],   -- UN hazmat classes present at source site
    buffer_distance_m          NUMERIC(10, 2),

    -- Data provenance
    valid_from       DATE NOT NULL,
    valid_to         DATE,
    source           TEXT NOT NULL,
    source_version   TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    geometry         GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX idx_env_zones_geom      ON spatial.environmental_zones USING GIST(geometry);
CREATE INDEX idx_env_zones_city      ON spatial.environmental_zones(city_id);
CREATE INDEX idx_env_zones_type      ON spatial.environmental_zones(city_id, zone_type);
CREATE INDEX idx_env_zones_flood     ON spatial.environmental_zones(city_id, zone_code)
    WHERE zone_type = 'flood';
CREATE INDEX idx_env_zones_industrial ON spatial.environmental_zones(city_id)
    WHERE zone_type = 'industrial_buffer';

COMMENT ON TABLE spatial.environmental_zones IS
    'Environmental constraint and hazard zones. '
    'flood zones drive hard constraint exclusion in apply_hard_constraints(). '
    'industrial_buffer zones enforce minimum separation for sensitive facilities. '
    'other zone types available for future facility-specific constraint rules.';

-- -----------------------------------------------------------------------------
-- demographic_grid
-- Regular grid of population statistics derived from census data.
-- Grid cells align to census tract / block group boundaries where possible.
-- Used directly in _score_gravity_catchment as demand origin points.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.demographic_grid (
    cell_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- Population counts
    population           INTEGER NOT NULL DEFAULT 0,
    population_density   NUMERIC(12, 4),   -- persons per km²
    household_count      INTEGER,
    avg_household_size   NUMERIC(5, 2),

    -- Age structure — critical for health demand weighting
    -- The gravity model weights elderly population 3x (AHRQ hospitalisation ratio)
    pop_under_18         INTEGER DEFAULT 0,
    pop_18_to_44         INTEGER DEFAULT 0,
    pop_45_to_64         INTEGER DEFAULT 0,
    pop_65_to_74         INTEGER DEFAULT 0,
    pop_75_plus          INTEGER DEFAULT 0,
    -- Convenience column: total elderly (65+) used directly in engine
    elderly_population   INTEGER GENERATED ALWAYS AS (
                             COALESCE(pop_65_to_74, 0) + COALESCE(pop_75_plus, 0)
                         ) STORED,

    -- Socioeconomic indicators
    median_household_income    NUMERIC(12, 2),
    pct_below_poverty_line     NUMERIC(5, 2),
    pct_no_vehicle             NUMERIC(5, 2),  -- households without a car
    pct_uninsured              NUMERIC(5, 2),  -- no health insurance

    -- Data provenance
    census_year          SMALLINT NOT NULL,
    census_tract_id      TEXT,              -- FIPS code or local equivalent
    source               TEXT DEFAULT 'US_CENSUS_ACS',
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    geometry             GEOMETRY(POLYGON, 4326) NOT NULL
);

CREATE INDEX idx_demographic_grid_geom ON spatial.demographic_grid USING GIST(geometry);
CREATE INDEX idx_demographic_grid_city ON spatial.demographic_grid(city_id);
CREATE INDEX idx_demographic_grid_pop  ON spatial.demographic_grid(city_id, population);

COMMENT ON TABLE spatial.demographic_grid IS
    'Population grid derived from census data. '
    'elderly_population is a generated column (pop_65_to_74 + pop_75_plus). '
    'Used as demand origin points in _score_gravity_catchment.';

-- -----------------------------------------------------------------------------
-- health_demand_grid
-- CDC PLACES disease prevalence joined to census tract geometries.
-- This is the richest demand signal in the hospital suitability model.
--
-- Populated by the data pipeline that:
--   1. Downloads CDC PLACES API data (census tract level)
--   2. Joins to demographic_grid on census_tract_id / geometry
--   3. Adds ed_visit_rate from hospital discharge records (where available)
--
-- Design note: kept separate from demographic_grid because:
--   - CDC PLACES updates annually on a different schedule from census
--   - Not all cities will have all disease prevalence columns populated
--   - The join logic is complex and best handled in the pipeline layer
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.health_demand_grid (
    cell_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- Link to demographic grid cell
    demographic_cell_id  UUID REFERENCES spatial.demographic_grid(cell_id),
    census_tract_id      TEXT,

    -- Population base (duplicated from demographic_grid for query convenience
    -- — avoids joins in the hot path of _score_gravity_catchment)
    population           INTEGER NOT NULL DEFAULT 0,
    elderly_population   INTEGER NOT NULL DEFAULT 0,

    -- ED utilisation: revealed demand signal
    -- Source: hospital discharge records / state ED databases
    -- Units: ED visits per 1,000 residents per year
    -- NULL when data unavailable — engine handles gracefully
    ed_visit_rate        NUMERIC(8, 3),

    -- CDC PLACES disease prevalence (% of adult population, age-adjusted)
    -- These are the exact column names the engine references in DISEASE_WEIGHTS
    -- NULL = data not available for this tract (engine fills with 0 and warns)
    prevalence_heart_disease   NUMERIC(5, 2),
    prevalence_diabetes        NUMERIC(5, 2),
    prevalence_copd            NUMERIC(5, 2),
    prevalence_stroke          NUMERIC(5, 2),
    prevalence_obesity         NUMERIC(5, 2),
    prevalence_mental_health   NUMERIC(5, 2),

    -- Additional CDC PLACES indicators (available but not in current engine)
    -- Included now so the schema is forward-compatible with future scoring criteria
    prevalence_hypertension    NUMERIC(5, 2),
    prevalence_cancer          NUMERIC(5, 2),
    prevalence_kidney_disease  NUMERIC(5, 2),
    prevalence_arthritis       NUMERIC(5, 2),

    -- Access barriers (affect revealed vs modelled demand gap)
    pct_no_health_insurance    NUMERIC(5, 2),
    pct_no_usual_care_source   NUMERIC(5, 2),  -- no regular doctor
    pct_delayed_care_cost      NUMERIC(5, 2),  -- delayed care due to cost

    -- Data currency
    cdc_places_year      SMALLINT NOT NULL,
    ed_data_year         SMALLINT,
    source               TEXT DEFAULT 'CDC_PLACES',
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Spatial — same geometry as demographic grid cell
    geometry             GEOMETRY(POLYGON, 4326) NOT NULL
);

CREATE INDEX idx_health_grid_geom      ON spatial.health_demand_grid USING GIST(geometry);
CREATE INDEX idx_health_grid_city      ON spatial.health_demand_grid(city_id);
CREATE INDEX idx_health_grid_tract     ON spatial.health_demand_grid(city_id, census_tract_id);
CREATE INDEX idx_health_grid_demand    ON spatial.health_demand_grid(city_id, ed_visit_rate);

COMMENT ON TABLE spatial.health_demand_grid IS
    'CDC PLACES disease prevalence joined to census tract geometries. '
    'Drives score_health_demand and _score_gravity_catchment in hospital.py. '
    'elderly_population is denormalised from demographic_grid for query performance. '
    'NULL prevalence values are handled gracefully by the engine (filled with 0).';
