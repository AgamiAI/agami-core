-- agami-core serving schema — the semantic model, served from the DB instead of local YAML files.
--
-- Shape mirrors the in-memory Organization tree (semantic_model/models.py) under an
-- org -> datasource -> subject-area hierarchy. Each object row carries the structural/key columns
-- a tool queries on PLUS a `doc` (the object's JSON) so the loader rebuilds the EXACT pydantic
-- object losslessly — no per-field column sprawl, and YAML stays the source of truth.
--
-- Portable DDL: TEXT/INTEGER only, app-minted keys (no SERIAL/JSONB) so the same file runs on
-- SQLite (tests / small self-host) and Postgres (prod).
--
-- table -> tool backing map:
--   organization, subject_area, model_table, metric, entity, relationship, memory
--                              -> get_datasource_schema (subject_area.table_count + model_table.est_row_count
--                                 drive the full/summary/index sizing + large_tables; relationship rows feed
--                                 execute_sql's internal pre_flight_check; entity.value_pattern feeds the
--                                 folded identify_entity)
--   prompt_example             -> get_prompt_examples (scoped by datasource+area, ranked app-side)
--   model_version              -> the execute_sql trust receipt's version pin
--   (list_datasources reads the set of `datasource` rows; execute_sql/log_feedback write runtime rows — 002)

CREATE TABLE organization (
    datasource  TEXT PRIMARY KEY,   -- the profile/datasource id (single-tenant: one org per datasource)
    org_name    TEXT NOT NULL,
    description TEXT,
    doc         TEXT NOT NULL       -- org-level JSON: version, fiscal_year_start_month, glossary,
                                    -- storage_connections, cross_subject_area_{relationships,metrics,entities}
);

CREATE TABLE subject_area (
    datasource          TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT,
    default_time_window TEXT,
    table_count         INTEGER NOT NULL DEFAULT 0,  -- sizing metadata (full <=12 / summary <=50 / index)
    doc                 TEXT NOT NULL,
    PRIMARY KEY (datasource, name)
);

CREATE TABLE model_table (
    datasource    TEXT NOT NULL,
    area          TEXT NOT NULL,
    name          TEXT NOT NULL,
    est_row_count INTEGER,          -- sizing metadata: >= 1e6 surfaces in large_tables
    doc           TEXT NOT NULL,
    PRIMARY KEY (datasource, area, name)
);

CREATE TABLE metric (
    datasource TEXT NOT NULL,
    area       TEXT,               -- NULL = org-level (cross-subject-area) metric
    name       TEXT NOT NULL,
    doc        TEXT NOT NULL,
    PRIMARY KEY (datasource, area, name)
);

CREATE TABLE entity (
    datasource    TEXT NOT NULL,
    area          TEXT,            -- NULL = org-level (cross-subject-area) entity
    name          TEXT NOT NULL,
    value_pattern TEXT,            -- feeds the folded identify_entity matching
    doc           TEXT NOT NULL,
    PRIMARY KEY (datasource, area, name)
);

CREATE TABLE relationship (
    datasource TEXT NOT NULL,
    area       TEXT,               -- NULL = cross-subject-area / cross-datasource relationship
    name       TEXT NOT NULL,      -- "from->to"
    doc        TEXT NOT NULL,
    PRIMARY KEY (datasource, area, name)
);

CREATE TABLE prompt_example (
    datasource TEXT NOT NULL,
    area       TEXT,               -- NULL = org-level cross-datasource example bucket
    id         TEXT NOT NULL,
    question   TEXT NOT NULL,
    doc        TEXT NOT NULL,
    embedding  TEXT,               -- optional, off by default: deploy-time vector as JSON text (no pgvector)
    PRIMARY KEY (datasource, id)
);

CREATE TABLE memory (
    datasource TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- 'organization' (ORGANIZATION.md) | 'user' (USER_MEMORY.md)
    content    TEXT,
    PRIMARY KEY (datasource, kind)
);

CREATE TABLE model_version (
    datasource TEXT NOT NULL,
    version    TEXT NOT NULL,      -- the snapshot content hash the receipt pins
    created_at TEXT,
    PRIMARY KEY (datasource, version)
);
