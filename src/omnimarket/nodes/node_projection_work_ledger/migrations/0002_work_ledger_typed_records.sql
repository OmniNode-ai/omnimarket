-- OMN-20001: follows 0001_grant_omninode_runtime_work_ledger.sql.
-- Source declaration only. Source lands before its byte-identical infra vendor.
-- Before a v2 insert, the writer reconciles every existing row with its configured
-- ledger_id and the producer-owned UUID5 helper. No second identity algorithm
-- is implemented in SQL. The unique guard exists BEFORE any backfill or insert.
ALTER TABLE omninode_internal.work_ledger_rows
    ADD COLUMN IF NOT EXISTS event_id UUID,
    ADD COLUMN IF NOT EXISTS record TEXT,
    ADD COLUMN IF NOT EXISTS provenance_kind TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_ledger_rows_event_id
    ON omninode_internal.work_ledger_rows (event_id) WHERE event_id IS NOT NULL;
ALTER TABLE omninode_internal.work_ledger_state
    ADD COLUMN IF NOT EXISTS question_status TEXT,
    ADD COLUMN IF NOT EXISTS question_event UUID,
    ADD COLUMN IF NOT EXISTS record TEXT;
