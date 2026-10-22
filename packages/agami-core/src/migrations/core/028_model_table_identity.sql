-- A table's identity is where it lives in the warehouse, not its bare name (#258, #337).
--
-- `model_table` was keyed (org, datasource, area, name), so `sales_data.orders` and `staging.orders`
-- in one subject area landed on one key and the deploy failed. The schema, and the catalog (database
-- / project) a hand-curated multi-database model may name, join the key. Both are '' when the model
-- does not say, never NULL: Postgres forbids NULL in a primary-key column.
--
-- Rebuilt rather than altered, because SQLite cannot change a primary key (same shape as 005).
-- Existing rows copy over with '' in both, which is still unique (the old key was) and still reads
-- correctly: the identity is rebuilt from `doc`, and the next deploy rewrites the rows in full.

CREATE TABLE model_table_new (
    org_id        TEXT NOT NULL DEFAULT 'local',
    datasource    TEXT NOT NULL,
    area          TEXT NOT NULL,
    catalog_name  TEXT NOT NULL DEFAULT '',
    schema_name   TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    est_row_count INTEGER,
    doc           TEXT NOT NULL,
    PRIMARY KEY (org_id, datasource, area, catalog_name, schema_name, name)
);

INSERT INTO model_table_new (org_id, datasource, area, name, est_row_count, doc)
    SELECT org_id, datasource, area, name, est_row_count, doc FROM model_table;

DROP TABLE model_table;
ALTER TABLE model_table_new RENAME TO model_table;
