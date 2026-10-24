-- OMN-21334: retain the producer stamps that close a compaction barrier.
-- Legacy rows remain NULL; no high-water is inferred from timestamps.
-- Additive and replayable, following the existing projection migrations.

ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS content_record_id TEXT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS stream TEXT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS producer_seq BIGINT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS record_type TEXT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS barrier_id TEXT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS high_water BIGINT;
ALTER TABLE omninode_internal.session_content
    ADD COLUMN IF NOT EXISTS content_capture TEXT;

CREATE INDEX IF NOT EXISTS idx_session_content_session_stream_sequence
    ON omninode_internal.session_content (session_id, stream, producer_seq)
    WHERE producer_seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_session_content_session_barrier
    ON omninode_internal.session_content (session_id, barrier_id, stream)
    WHERE content_kind = 'barrier';

SELECT 1 / count(*) AS session_content_content_record_id_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'content_record_id'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_stream_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'stream'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_producer_seq_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'producer_seq'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_record_type_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'record_type'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_barrier_id_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'barrier_id'
   AND data_type = 'text'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_high_water_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'high_water'
   AND data_type = 'bigint'
   AND is_nullable = 'YES';

SELECT 1 / count(*) AS session_content_content_capture_shape_assertion
  FROM information_schema.columns
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'session_content'
   AND column_name = 'content_capture'
   AND data_type = 'text'
   AND is_nullable = 'YES';
