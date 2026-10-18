-- OMN-21015: the local store's counterpart of migration 0003's new key, run once by the store step
-- omn21015_usage_by_model_day_tier_share when a store's usage_by_model_day is still keyed on three columns.
-- SQLite cannot change a primary key in place: the step has created usage_by_model_day_omn21015 with the new key,
-- and these statements copy every row into it under not_tier_routed with its cursor kept, replace the old table and
-- move the cursor sequence past every kept cursor. call_share stays NULL until the row's day takes a new call, as on
-- Postgres. The step runs each statement on its own, inside its one write transaction; the cursor triggers, which
-- go with the dropped table, are created again by the step afterwards.
INSERT INTO usage_by_model_day_omn21015 (
    tenant_id, usage_day, model_id, input_tokens, output_tokens, cost_usd,
    measured_cost_usd, unmeasured_call_count, call_count, updated_at, projection_cursor
)
SELECT tenant_id, usage_day, model_id, input_tokens, output_tokens, cost_usd,
       measured_cost_usd, unmeasured_call_count, call_count, updated_at, projection_cursor
FROM usage_by_model_day;

DROP TABLE usage_by_model_day;

ALTER TABLE usage_by_model_day_omn21015 RENAME TO usage_by_model_day;

UPDATE usage_by_model_day_projection_cursor_seq
SET last_value = MAX(
    last_value,
    (SELECT COALESCE(MAX(projection_cursor), 0) FROM usage_by_model_day)
);
