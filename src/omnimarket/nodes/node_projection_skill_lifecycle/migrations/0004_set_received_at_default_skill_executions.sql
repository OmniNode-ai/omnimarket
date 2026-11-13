-- OMN-21933: the writer's INSERT omits received_at. 0001 adds the column with
-- DEFAULT NOW() only through ADD COLUMN IF NOT EXISTS, a no-op when the column
-- already exists, so a pre-existing column without a default would refuse every
-- insert. 0001 is already applied and stays byte-identical to the omnibase_infra
-- vendored copy; the default is its own step here.
ALTER TABLE public.skill_executions ALTER COLUMN received_at SET DEFAULT NOW();
