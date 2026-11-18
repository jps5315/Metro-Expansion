-- =============================================================================
-- 001_extensions.sql
-- PostGIS extensions and configuration
-- Run this first — all other migrations depend on these extensions.
-- =============================================================================

-- Core spatial extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- Raster support (terrain_grid, slope analysis)
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- Topology support (future network analysis)
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- UUID generation for primary keys
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- pg_trgm for fuzzy text search on facility names, zoning codes
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- btree_gist allows GiST indexes on scalar types alongside geometry
-- Required for exclusion constraints combining geometry + time ranges
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- -----------------------------------------------------------------------------
-- Schema namespaces
-- We separate concerns into three schemas:
--   spatial   — raw and processed spatial layers (input data)
--   analysis  — suitability engine outputs and scenarios
--   audit     — immutable audit log, never updated or deleted
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS spatial;
CREATE SCHEMA IF NOT EXISTS analysis;
CREATE SCHEMA IF NOT EXISTS audit;

-- Set search path for the application role
-- Application connects as mep_app — it sees all three schemas
ALTER DATABASE mep_db SET search_path TO spatial, analysis, audit, public;
