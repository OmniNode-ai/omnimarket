-- OMN-19968: make the input_hash dedup index inferable by ON CONFLICT.
--
-- 0001 created ux_llm_call_metrics_input_hash as a PARTIAL unique index
-- (WHERE input_hash IS NOT NULL). PostgreSQL only infers a partial index for
-- ON CONFLICT (input_hash) when the statement repeats the predicate, and the
-- store-neutral writer (a plan rendering ``ON CONFLICT (input_hash) DO NOTHING``,
-- shared with the SQLite local adapter) does not, so the write failed with
-- "there is no unique or exclusion constraint matching the ON CONFLICT
-- specification". A plain unique index is equivalent for dedup: PostgreSQL
-- treats NULLs as distinct, so legacy NULL input_hash rows stay unconstrained.
DROP INDEX IF EXISTS ux_llm_call_metrics_input_hash;
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_call_metrics_input_hash
    ON llm_call_metrics (input_hash);
