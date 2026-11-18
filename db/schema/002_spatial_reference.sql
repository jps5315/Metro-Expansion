-- =============================================================================
-- 002_spatial_reference.sql
-- Administrative boundaries and city registry
--
-- Purpose: Every analysis run is scoped to a city. This table is the anchor
-- that all other spatial data references. The projection strategy (UTM zone,
-- LAEA centre etc.) is derived from city_boundaries.geometry at runtime by
-- the Python engine — we store the result here for audit and reproducibility.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- cities
-- Registry of cities/municipalities onboarded to the MEP platform.
-- One row per city regardless of how many analyses have been run.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.cities (
    city_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_name       TEXT NOT NULL,
    country_code    CHAR(2) NOT NULL,          -- ISO 3166-1 alpha-2
    state_province  TEXT,
    timezone        TEXT NOT NULL,             -- IANA timezone e.g. 'America/New_York'
    population      BIGINT,                    -- latest known population
    area_km2        NUMERIC(12, 4),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT cities_name_country_unique UNIQUE (city_name, country_code)
);

COMMENT ON TABLE spatial.cities IS
    'Registry of cities onboarded to the MEP platform. '
    'One row per city — the anchor for all spatial data and analysis runs.';

-- -----------------------------------------------------------------------------
-- city_boundaries
-- Administrative boundary polygons for each city.
-- Stored in WGS84 (EPSG:4326) — the engine reprojects at runtime.
-- Multiple boundary versions are supported (admin boundary changes over time).
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.city_boundaries (
    boundary_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id         UUID NOT NULL REFERENCES spatial.cities(city_id),
    boundary_type   TEXT NOT NULL DEFAULT 'municipal',
                    -- 'municipal' | 'metropolitan' | 'planning_zone' | 'study_area'
    valid_from      DATE NOT NULL,
    valid_to        DATE,                      -- NULL = currently active
    source          TEXT,                      -- e.g. 'US_Census_TIGER_2023'
    geometry        GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT city_boundaries_no_overlap EXCLUDE USING GIST (
        city_id WITH =,
        boundary_type WITH =,
        TSRANGE(valid_from, valid_to, '[)') WITH &&,
        geometry WITH &&
    )
);

CREATE INDEX idx_city_boundaries_city_id  ON spatial.city_boundaries(city_id);
CREATE INDEX idx_city_boundaries_geom     ON spatial.city_boundaries USING GIST(geometry);
CREATE INDEX idx_city_boundaries_active   ON spatial.city_boundaries(city_id, boundary_type)
    WHERE valid_to IS NULL;

COMMENT ON TABLE spatial.city_boundaries IS
    'Administrative boundary polygons. Stored WGS84 — engine reprojects at runtime. '
    'Temporal validity tracks boundary changes. EXCLUDE constraint prevents overlapping '
    'active boundaries of the same type for the same city.';

-- -----------------------------------------------------------------------------
-- projection_audit
-- Records the projection strategy selected for each analysis run.
-- Critical for reproducibility — analysts need to know which CRS was used
-- when results were produced. Especially important for custom proj4 CRS
-- (LAEA, AEQD) which have no EPSG code.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.projection_audit (
    projection_audit_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Study area characterisation
    centroid_lon         NUMERIC(10, 6) NOT NULL,
    centroid_lat         NUMERIC(10, 6) NOT NULL,
    diagonal_km          NUMERIC(10, 2),
    lon_span_deg         NUMERIC(8, 4),
    crosses_utm_boundary BOOLEAN,
    crosses_antimeridian BOOLEAN,

    -- Selected CRS details
    distance_crs_name    TEXT NOT NULL,
    distance_crs_family  TEXT NOT NULL,   -- 'UTM' | 'Azimuthal Equidistant' | ...
    distance_crs_epsg    INTEGER,         -- NULL for custom proj4 CRS
    distance_crs_proj4   TEXT,            -- full proj4 string for custom CRS

    area_crs_proj4       TEXT NOT NULL,   -- always custom LAEA — no EPSG
    angle_crs_name       TEXT NOT NULL,
    angle_crs_epsg       INTEGER,
    angle_crs_proj4      TEXT
);

CREATE INDEX idx_projection_audit_city ON spatial.projection_audit(city_id);

COMMENT ON TABLE spatial.projection_audit IS
    'Records the runtime-selected CRS strategy for every analysis run. '
    'Enables reproducibility — an analyst 2 years later can recreate the exact '
    'projection used. Especially important for custom proj4 strings (LAEA, AEQD) '
    'that have no EPSG code.';
