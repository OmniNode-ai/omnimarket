-- OMN-20226: Compression and Cache hit rate, served beside the other headline
-- figures so the Overview binds them instead of computing them.
--
-- compression_ratio is raw input tokens over compressed input tokens, and
-- cache_hit_rate is runs_cache_answered over runs_total, each as decimal text.
-- Nothing on the delegate path measures compression or a semantic-cache answer
-- yet, so the fold writes NULL for all three until a producer records them.
-- Nullable with no default: a row with no measurement reads NULL, never zero.

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS compression_ratio TEXT;

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS cache_hit_rate TEXT;

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_cache_answered INTEGER;
