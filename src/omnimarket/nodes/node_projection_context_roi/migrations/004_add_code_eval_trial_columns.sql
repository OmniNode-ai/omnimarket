-- OMN-23628: context_roi_scores keeps one row per code-eval try.
--
-- The columns below are the per-try fields ModelAttemptReductionRow gained for
-- the local-model code eval: the check verdict and, when the try could not be
-- scored, why; whether the row is a try or a control; how the try ended; the
-- suite digest; the task's size band; the sequence the try belongs to and its
-- ordinal; the hash of its diff; the two limits in force; and the time split.
--
-- Additive only. Every column carries a default, so a row written before this
-- migration reads as a try of size band S with zero limits and timings, and
-- the existing correlation_id key, indexes and tenant policy are unchanged.
-- No second table is created: the code eval is not a second result store.

ALTER TABLE context_roi_scores
    ADD COLUMN IF NOT EXISTS check_verdict TEXT,
    ADD COLUMN IF NOT EXISTS unscorable_source TEXT,
    ADD COLUMN IF NOT EXISTS row_kind TEXT NOT NULL DEFAULT 'try',
    ADD COLUMN IF NOT EXISTS try_end TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS suite_digest TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS size_band TEXT NOT NULL DEFAULT 'S',
    ADD COLUMN IF NOT EXISTS sequence_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS try_ordinal INTEGER NOT NULL DEFAULT 0 CHECK (try_ordinal >= 0),
    ADD COLUMN IF NOT EXISTS diff_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS max_turns INTEGER NOT NULL DEFAULT 0 CHECK (max_turns >= 0),
    ADD COLUMN IF NOT EXISTS try_timeout_s NUMERIC(12, 3) NOT NULL DEFAULT 0 CHECK (try_timeout_s >= 0),
    ADD COLUMN IF NOT EXISTS generation_s NUMERIC(12, 3) NOT NULL DEFAULT 0 CHECK (generation_s >= 0),
    ADD COLUMN IF NOT EXISTS env_sync_s NUMERIC(12, 3) NOT NULL DEFAULT 0 CHECK (env_sync_s >= 0),
    ADD COLUMN IF NOT EXISTS check_s NUMERIC(12, 3) NOT NULL DEFAULT 0 CHECK (check_s >= 0);
