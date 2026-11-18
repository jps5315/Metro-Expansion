-- =============================================================================
-- 004_infrastructure.sql
-- Road network, utilities, transit, and terrain
--
-- These are the infrastructure layers the suitability engine uses for:
--   - Hard constraint: road access check
--   - Scoring: road_access, emergency_access, public_transit
--   - Scoring: utility_coverage
--   - Scoring: flat_terrain
--   - Flood resilience: road_flood_events joins to road_network
-- =============================================================================

-- -----------------------------------------------------------------------------
-- road_network
-- Line geometry for roads. Sourced from OSM via osmnx ingestion pipeline.
-- road_class follows OSM highway tag hierarchy.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.road_network (
    road_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id            UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- OSM source identifiers
    osm_way_id         BIGINT,
    osm_node_from      BIGINT,
    osm_node_to        BIGINT,

    -- Road classification (OSM highway tag)
    road_class         TEXT NOT NULL,
    -- motorway | trunk | primary | secondary | tertiary | residential |
    -- service | footway | cycleway | path

    road_name          TEXT,
    speed_limit_kmh    SMALLINT,
    lanes              SMALLINT,
    is_oneway          BOOLEAN DEFAULT FALSE,
    surface            TEXT,          -- 'asphalt' | 'concrete' | 'gravel' | ...
    bridge             BOOLEAN DEFAULT FALSE,
    tunnel             BOOLEAN DEFAULT FALSE,

    -- For emergency routing
    is_emergency_route BOOLEAN DEFAULT FALSE,
    emergency_class    TEXT,          -- 'primary_response' | 'evacuation' | NULL

    -- Data currency
    osm_version        INTEGER,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Geometry: line in WGS84
    geometry           GEOMETRY(LINESTRING, 4326) NOT NULL
);

CREATE INDEX idx_road_network_geom      ON spatial.road_network USING GIST(geometry);
CREATE INDEX idx_road_network_city      ON spatial.road_network(city_id);
CREATE INDEX idx_road_network_class     ON spatial.road_network(city_id, road_class);
CREATE INDEX idx_road_network_emergency ON spatial.road_network(city_id)
    WHERE is_emergency_route = TRUE;

COMMENT ON TABLE spatial.road_network IS
    'Road network lines sourced from OSM. Used for road access hard constraint '
    'and proximity scoring. road_flood_events FK references road_id here.';

-- -----------------------------------------------------------------------------
-- road_flood_events
-- Historical flood events recorded on specific road segments.
-- This is the data source for score_access_resilience.
--
-- Sources:
--   - NOAA Storm Events Database (severe weather events with road impacts)
--   - FEMA NFIP claims data (insurance claims indicate flood extent)
--   - Local agency reports (city/county flood damage records)
--
-- One row per (road_segment × flood_event). A single flood event (e.g.
-- Hurricane Sandy) may produce thousands of rows — one per affected segment.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.road_flood_events (
    event_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    road_segment_id       UUID NOT NULL REFERENCES spatial.road_network(road_id),
    city_id               UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- Event timing
    event_date            DATE NOT NULL,
    event_start_datetime  TIMESTAMPTZ,
    event_end_datetime    TIMESTAMPTZ,
    duration_hours        NUMERIC(8, 2),

    -- Severity classification
    -- 'minor'    road passable with difficulty, no formal closure
    -- 'moderate' road closed, alternate routes available
    -- 'major'    neighbourhood isolated, all routes blocked
    -- 'extreme'  multi-day isolation, sustained emergency impact
    severity_class        TEXT NOT NULL
                          CHECK (severity_class IN ('minor','moderate','major','extreme')),

    water_depth_cm        NUMERIC(7, 2),  -- observed/estimated flood depth at road
    closure_hours         NUMERIC(8, 2),  -- how long road was actually closed

    -- Declared emergency flag
    -- TRUE when this event coincided with a declared state/federal emergency.
    -- These events are weighted more heavily in score_access_resilience because
    -- floods that coincide with declared emergencies are the exact scenarios
    -- where hospital access and medical demand spike simultaneously.
    declared_emergency    BOOLEAN NOT NULL DEFAULT FALSE,
    emergency_declaration_id TEXT,        -- FEMA/state declaration reference number

    -- Source provenance
    source                TEXT NOT NULL,
    -- 'NOAA_STORM_EVENTS' | 'FEMA_NFIP' | 'LOCAL_AGENCY' | 'MANUAL'
    source_event_id       TEXT,           -- original ID in the source dataset
    notes                 TEXT,

    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_road_flood_city        ON spatial.road_flood_events(city_id);
CREATE INDEX idx_road_flood_segment     ON spatial.road_flood_events(road_segment_id);
CREATE INDEX idx_road_flood_date        ON spatial.road_flood_events(event_date);
CREATE INDEX idx_road_flood_severity    ON spatial.road_flood_events(city_id, severity_class);
CREATE INDEX idx_road_flood_emergency   ON spatial.road_flood_events(city_id)
    WHERE declared_emergency = TRUE;
-- Composite index used by the access resilience query in hospital.py
CREATE INDEX idx_road_flood_analysis    ON spatial.road_flood_events(city_id, road_segment_id, event_date);

COMMENT ON TABLE spatial.road_flood_events IS
    'Historical flood events on road segments. Source for score_access_resilience. '
    'declared_emergency rows are weighted 1.5x because they represent the worst-case '
    'scenario where flood access failure coincides with peak hospital demand.';

-- -----------------------------------------------------------------------------
-- utility_coverage
-- Polygon coverage areas for utility networks (water, sewage, power, gas).
-- Used for score_utility_coverage — does the parcel have service?
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.utility_coverage (
    coverage_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),

    utility_type     TEXT NOT NULL,
    -- 'water' | 'sewage' | 'power' | 'gas' | 'telecoms'

    -- Service level / pressure zone (utility-type specific)
    service_level    TEXT,
    -- For water: 'potable' | 'recycled' | 'fire_suppression'
    -- For sewage: 'combined' | 'separate_storm' | 'separate_sanitary'
    -- For power: 'primary' | 'secondary'

    provider_name    TEXT,
    capacity_pct     NUMERIC(5, 2),  -- current utilisation 0–100%
    pressure_zone    TEXT,

    -- Data currency
    valid_from       DATE NOT NULL,
    valid_to         DATE,
    source           TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    geometry         GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX idx_utility_coverage_geom ON spatial.utility_coverage USING GIST(geometry);
CREATE INDEX idx_utility_coverage_city ON spatial.utility_coverage(city_id);
CREATE INDEX idx_utility_coverage_type ON spatial.utility_coverage(city_id, utility_type);

-- -----------------------------------------------------------------------------
-- terrain_grid
-- Regular grid of terrain attributes derived from DEM (SRTM 30m).
-- Each cell stores slope, aspect, and elevation for use in scoring.
-- grid_resolution_m records the DEM source resolution.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.terrain_grid (
    cell_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),

    elevation_m          NUMERIC(8, 2),
    slope_degrees        NUMERIC(6, 3) NOT NULL,
    aspect_degrees       NUMERIC(7, 3),   -- 0–360, north = 0

    -- Derived terrain classes
    terrain_class        TEXT,
    -- 'flat' (<2°) | 'gentle' (2-5°) | 'moderate' (5-15°) | 'steep' (>15°)

    -- DEM source metadata
    dem_source           TEXT DEFAULT 'SRTM_30m',
    grid_resolution_m    NUMERIC(8, 2) DEFAULT 30.0,
    processed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    geometry             GEOMETRY(POLYGON, 4326) NOT NULL
);

CREATE INDEX idx_terrain_grid_geom ON spatial.terrain_grid USING GIST(geometry);
CREATE INDEX idx_terrain_grid_city ON spatial.terrain_grid(city_id);
CREATE INDEX idx_terrain_grid_slope ON spatial.terrain_grid(city_id, slope_degrees);

-- -----------------------------------------------------------------------------
-- existing_facilities
-- Point layer of all existing facilities: hospitals, schools, GPs,
-- transit stops, etc. Used throughout the suitability engine.
-- A single table with facility_type discrimination is used over separate
-- tables to allow cross-type spatial queries (e.g. find all facilities
-- of any type within 500m of a candidate parcel).
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.existing_facilities (
    facility_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),

    facility_type    TEXT NOT NULL,
    -- 'hospital' | 'school' | 'university' | 'gp_clinic' |
    -- 'community_health_centre' | 'urgent_care' |
    -- 'bus_stop' | 'metro_station' | 'train_station' |
    -- 'fire_station' | 'police_station' | 'park' | 'library'

    facility_name    TEXT,
    operator         TEXT,
    is_public        BOOLEAN DEFAULT TRUE,
    is_operational   BOOLEAN DEFAULT TRUE,

    -- Hospital-specific (NULL for non-hospital types)
    bed_count        INTEGER,
    icu_beds         INTEGER,
    emergency_dept   BOOLEAN,
    trauma_level     SMALLINT,       -- 1 (highest) to 5 (lowest), NULL if no ED

    -- School-specific (NULL for non-school types)
    school_level     TEXT,           -- 'primary' | 'secondary' | 'vocational'
    student_capacity INTEGER,

    -- Transit-specific
    transit_lines    TEXT[],         -- array of line/route names serving this stop
    daily_ridership  INTEGER,

    -- Data provenance
    source           TEXT,
    osm_node_id      BIGINT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from       DATE NOT NULL,
    valid_to         DATE,

    geometry         GEOMETRY(POINT, 4326) NOT NULL
);

CREATE INDEX idx_existing_facilities_geom ON spatial.existing_facilities USING GIST(geometry);
CREATE INDEX idx_existing_facilities_city ON spatial.existing_facilities(city_id);
CREATE INDEX idx_existing_facilities_type ON spatial.existing_facilities(city_id, facility_type);
CREATE INDEX idx_existing_facilities_hosp ON spatial.existing_facilities(city_id)
    WHERE facility_type = 'hospital';
CREATE INDEX idx_existing_facilities_transit ON spatial.existing_facilities(city_id)
    WHERE facility_type IN ('bus_stop','metro_station','train_station');
CREATE INDEX idx_existing_facilities_primary_care ON spatial.existing_facilities(city_id)
    WHERE facility_type IN ('gp_clinic','community_health_centre','urgent_care');
