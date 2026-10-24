-- OMN-21334: retain the applied lineage sequence for compaction barriers.
-- This table carries only lineage; content and both barriers use session_content.
-- NULL on legacy events, with no sequence invented from the ingest cursor.

ALTER TABLE omninode_internal.claude_hook_events
    ADD COLUMN IF NOT EXISTS producer_seq BIGINT;

CREATE INDEX IF NOT EXISTS idx_claude_hook_events_session_sequence
    ON omninode_internal.claude_hook_events (session_id, producer_seq)
    WHERE producer_seq IS NOT NULL;

SELECT 1 / count(*) AS claude_hook_events_producer_seq_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'claude_hook_events'
   AND column_name = 'producer_seq'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';
