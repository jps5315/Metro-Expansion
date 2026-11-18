-- =============================================================================
-- 003_land_parcels.sql
-- Land parcels — the core unit of suitability analysis
--
-- Every suitability run starts with this table. Each row is one candidate
-- parcel. The engine filters and scores these rows; it never modifies them.
--
-- Design decisions:
-- 1.  Geometry stored WGS84 — reprojected at runtime by the engine.
-- 2.  area_m2 is stored as a pre-computed attribute from the source dataset.
--     The engine recomputes it in equal-area CRS at runtime for accuracy —
--     the stored value is used only for the initial coarse filter to avoid
--     loading millions of tiny parcels.
-- 3.  Zoning codes are stored as-is from the source (city-specific).
--     A separate zoning_lookup table maps them to standardised categories.
-- 4.  Parcel data is versioned — cities update their parcel data periodically.
--     We keep all versions so historical scenarios remain reproducible.
-- =============================================================================

CREATE TABLE spatial.land_parcels (
    parcel_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id                UUID NOT NULL REFERENCES spatial.cities(city_id),

    -- Source identifiers (from client dataset)
    source_parcel_id       TEXT NOT NULL,     -- client's own parcel ID
    source_dataset_version TEXT NOT NULL,     -- e.g. '2024-Q1'

    -- Zoning and land use
    zoning_code            TEXT NOT NULL,     -- raw code from client data
    zoning_category        TEXT,             -- standardised: 'residential','commercial' etc.
    land_use_category      TEXT,             -- current actual use
    land_use_subcategory   TEXT,

    -- Physical attributes
    area_m2                NUMERIC(14, 2),   -- pre-computed from source (coarse filter only)
    frontage_m             NUMERIC(10, 2),   -- road frontage length
    depth_m                NUMERIC(10, 2),

    -- Development status
    development_density    NUMERIC(6, 4),    -- 0.0 (vacant) to 1.0 (fully developed)
    building_coverage_pct  NUMERIC(5, 2),    -- % of parcel covered by existing buildings
    floor_area_ratio       NUMERIC(6, 3),    -- existing FAR
    is_vacant              BOOLEAN DEFAULT FALSE,
    is_publicly_owned      BOOLEAN DEFAULT FALSE,
    is_brownfield          BOOLEAN DEFAULT FALSE,

    -- Ownership and valuation (where available)
    owner_type             TEXT,             -- 'private' | 'municipal' | 'state' | 'federal'
    assessed_value         NUMERIC(16, 2),
    assessed_value_year    SMALLINT,
    assessed_value_currency CHAR(3) DEFAULT 'USD',

    -- Data provenance
    source_agency          TEXT,             -- e.g. 'NYC_DOF', 'Cook_County_Assessor'
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from             DATE NOT NULL,
    valid_to               DATE,

    -- Spatial
    geometry               GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,

    CONSTRAINT parcels_source_id_unique UNIQUE (city_id, source_parcel_id, source_dataset_version)
);

-- Spatial index — most queries hit this
CREATE INDEX idx_land_parcels_geom        ON spatial.land_parcels USING GIST(geometry);
-- City scoping — every query filters by city_id first
CREATE INDEX idx_land_parcels_city        ON spatial.land_parcels(city_id);
-- Zoning filter — applied early in hard constraints
CREATE INDEX idx_land_parcels_zoning      ON spatial.land_parcels(city_id, zoning_code);
-- Area pre-filter
CREATE INDEX idx_land_parcels_area        ON spatial.land_parcels(city_id, area_m2);
-- Current parcels only
CREATE INDEX idx_land_parcels_active      ON spatial.land_parcels(city_id)
    WHERE valid_to IS NULL;

COMMENT ON TABLE spatial.land_parcels IS
    'Individual land parcels — the atomic unit of suitability analysis. '
    'The engine filters and scores these but never modifies them. '
    'area_m2 is a coarse pre-computed value; the engine recomputes in equal-area CRS.';

-- -----------------------------------------------------------------------------
-- zoning_lookup
-- Maps city-specific raw zoning codes to standardised MEP categories.
-- Cities use wildly different zoning code schemes (NYC uses R1-R10, C1-C8;
-- Chicago uses B, C, D, E, M, R, T, etc.). This table normalises them.
-- -----------------------------------------------------------------------------
CREATE TABLE spatial.zoning_lookup (
    lookup_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id              UUID NOT NULL REFERENCES spatial.cities(city_id),
    raw_zoning_code      TEXT NOT NULL,

    -- Standardised MEP categories
    mep_category         TEXT NOT NULL,
    -- 'residential_low' | 'residential_high' | 'commercial' | 'institutional' |
    -- 'industrial_light' | 'industrial_heavy' | 'mixed_use' | 'open_space' |
    -- 'public_facility' | 'agricultural' | 'special_purpose'

    -- Which facility types are permitted under this zoning (array)
    permitted_facility_types  TEXT[],
    -- e.g. ARRAY['hospital','school','clinic'] for institutional zones

    conditional_facility_types TEXT[],
    -- Conditionally permitted — requires special approval

    notes                TEXT,

    CONSTRAINT zoning_lookup_unique UNIQUE (city_id, raw_zoning_code)
);

CREATE INDEX idx_zoning_lookup_city ON spatial.zoning_lookup(city_id);
CREATE INDEX idx_zoning_lookup_code ON spatial.zoning_lookup(city_id, raw_zoning_code);

COMMENT ON TABLE spatial.zoning_lookup IS
    'Maps city-specific raw zoning codes to standardised MEP categories and '
    'permitted facility types. Required because every city uses different zoning '
    'code nomenclature.';
