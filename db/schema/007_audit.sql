-- =============================================================================
-- 007_audit.sql
-- Immutable audit log
--
-- City planning decisions have legal and regulatory consequences. Every
-- significant action — running an analysis, changing weights, approving a site,
-- modifying a parcel record — must be traceable. This schema is append-only.
-- No UPDATE or DELETE is ever permitted on audit tables.
--
-- The audit log answers the question a committee will ask:
-- "On what date, with what data, using what weights, did your system recommend
--  this site, and who approved it?"
-- =============================================================================

CREATE TABLE audit.event_log (
    log_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Actor
    user_id          UUID,           -- NULL for system-generated events
    user_email       TEXT,           -- denormalised so log is self-contained
    client_ip        INET,
    session_id       TEXT,

    -- What happened
    event_type       TEXT NOT NULL,
    -- 'analysis_run_started'   | 'analysis_run_completed' | 'analysis_run_failed'
    -- 'site_proposed'          | 'site_shortlisted'       | 'site_approved'
    -- 'site_rejected'          | 'scenario_created'       | 'scenario_archived'
    -- 'weights_modified'       | 'constraint_modified'    | 'data_ingested'
    -- 'parcel_data_updated'    | 'user_login'             | 'export_generated'

    -- Object references
    city_id          UUID,
    scenario_id      UUID,
    run_id           UUID,
    parcel_id        UUID,
    site_id          UUID,

    -- Full before/after state for modifications (JSONB)
    -- NULL for create events (no "before" state)
    -- NULL for read events
    state_before     JSONB,
    state_after      JSONB,

    -- Free-form context
    description      TEXT,
    metadata         JSONB DEFAULT '{}'
);

-- Time-based queries: "what happened on date X?"
CREATE INDEX idx_audit_occurred    ON audit.event_log(occurred_at DESC);
-- Entity queries: "what happened to this scenario?"
CREATE INDEX idx_audit_scenario    ON audit.event_log(scenario_id) WHERE scenario_id IS NOT NULL;
CREATE INDEX idx_audit_run         ON audit.event_log(run_id)      WHERE run_id IS NOT NULL;
CREATE INDEX idx_audit_parcel      ON audit.event_log(parcel_id)   WHERE parcel_id IS NOT NULL;
CREATE INDEX idx_audit_user        ON audit.event_log(user_id)     WHERE user_id IS NOT NULL;
CREATE INDEX idx_audit_city        ON audit.event_log(city_id)     WHERE city_id IS NOT NULL;
CREATE INDEX idx_audit_event_type  ON audit.event_log(event_type, occurred_at DESC);

-- Prevent any modification to audit records
-- These triggers fire on UPDATE and DELETE and raise an exception
CREATE OR REPLACE FUNCTION audit.prevent_audit_modification()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Audit log is immutable. No UPDATE or DELETE permitted on audit.event_log. '
        'Event ID: %', OLD.log_id;
END;
$$;

CREATE TRIGGER trg_audit_no_update
    BEFORE UPDATE ON audit.event_log
    FOR EACH ROW EXECUTE FUNCTION audit.prevent_audit_modification();

CREATE TRIGGER trg_audit_no_delete
    BEFORE DELETE ON audit.event_log
    FOR EACH ROW EXECUTE FUNCTION audit.prevent_audit_modification();

COMMENT ON TABLE audit.event_log IS
    'Immutable append-only audit log. UPDATE and DELETE are blocked by triggers. '
    'Records every significant action in the system with full before/after state. '
    'Required for regulatory compliance — city planning decisions are legally accountable.';

-- =============================================================================
-- Convenience views
-- =============================================================================

-- Latest active boundary per city
CREATE VIEW spatial.v_city_active_boundaries AS
    SELECT cb.*, c.city_name, c.country_code
    FROM spatial.city_boundaries cb
    JOIN spatial.cities c ON c.city_id = cb.city_id
    WHERE cb.valid_to IS NULL;

-- Top-ranked candidates from each completed run
CREATE VIEW analysis.v_top_candidates AS
    SELECT
        ss.*,
        lp.zoning_code,
        lp.area_m2,
        lp.development_density,
        lp.geometry,
        ar.facility_type,
        ar.weights_used,
        es.scenario_name,
        es.city_id
    FROM analysis.suitability_scores ss
    JOIN analysis.analysis_runs ar     ON ar.run_id = ss.run_id
    JOIN analysis.expansion_scenarios es ON es.scenario_id = ar.scenario_id
    JOIN spatial.land_parcels lp       ON lp.parcel_id = ss.parcel_id
    WHERE ss.rank <= 20
      AND ss.was_eliminated = FALSE
      AND ar.status = 'completed';

-- Full decision trail for approved sites
CREATE VIEW analysis.v_approved_site_trail AS
    SELECT
        ps.site_id,
        ps.site_name,
        ps.facility_type,
        ps.final_score,
        ps.rank_in_run,
        ps.proposed_at,
        ps.approved_at,
        ps.estimated_cost_usd,
        ps.target_open_year,
        ps.planner_notes,
        es.scenario_name,
        c.city_name,
        c.country_code,
        lp.zoning_code,
        lp.area_m2,
        lp.geometry
    FROM analysis.proposed_sites ps
    JOIN analysis.expansion_scenarios es ON es.scenario_id = ps.scenario_id
    JOIN spatial.cities c               ON c.city_id = ps.city_id
    JOIN spatial.land_parcels lp        ON lp.parcel_id = ps.parcel_id
    WHERE ps.status = 'approved';

COMMENT ON VIEW analysis.v_approved_site_trail IS
    'Full decision trail for all approved sites — joins scores, scenarios, '
    'city info, and parcel geometry. Used for committee reporting and export.';

-- =============================================================================
-- Data quality anomaly log
-- Written by engine/validation/data_quality.py at ingestion time.
-- Immutable — same trigger protection as event_log.
-- =============================================================================
CREATE TABLE audit.anomaly_log (
    anomaly_log_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id          UUID NOT NULL REFERENCES spatial.cities(city_id),
    logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Which dataset was validated
    dataset_name     TEXT NOT NULL,
    source_file      TEXT,
    file_hash        TEXT,          -- SHA-256 of source file

    -- Summary counts
    validated_at     TIMESTAMPTZ NOT NULL,
    n_rows_checked   INTEGER NOT NULL,
    critical_count   INTEGER NOT NULL DEFAULT 0,
    warning_count    INTEGER NOT NULL DEFAULT 0,

    -- Outcome
    passed           BOOLEAN NOT NULL,
    blocked          BOOLEAN NOT NULL,

    -- Full report as JSONB — contains all anomaly details, affected IDs,
    -- recommendations. Stored in full so we have a complete record even if
    -- the anomaly catalogue changes in future versions.
    report_json      JSONB NOT NULL,

    -- Planner acknowledgement (required before analysis can run on blocked data)
    acknowledged_by  UUID,          -- FK to auth.users
    acknowledged_at  TIMESTAMPTZ,
    acknowledgement_note TEXT       -- planner's written justification for proceeding
);

CREATE INDEX idx_anomaly_log_city    ON audit.anomaly_log(city_id, logged_at DESC);
CREATE INDEX idx_anomaly_log_blocked ON audit.anomaly_log(city_id)
    WHERE blocked = TRUE AND acknowledged_at IS NULL;
CREATE INDEX idx_anomaly_log_dataset ON audit.anomaly_log(city_id, dataset_name);

-- Immutability trigger (same pattern as event_log)
CREATE TRIGGER trg_anomaly_no_update
    BEFORE UPDATE OF report_json, file_hash, critical_count, warning_count,
                     passed, blocked, logged_at
    ON audit.anomaly_log
    FOR EACH ROW EXECUTE FUNCTION audit.prevent_audit_modification();

COMMENT ON TABLE audit.anomaly_log IS
    'Immutable record of every data quality validation run. '
    'blocked=TRUE rows require planner acknowledgement before analysis proceeds. '
    'acknowledged_at + acknowledged_by form the legal defence record: '
    'the planner accepted responsibility for proceeding with anomalous data.';
