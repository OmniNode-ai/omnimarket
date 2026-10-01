-- OMN-20242. Owned by omnimarket.nodes.node_projection_delegation_disposition.
-- Answer content is never persisted here; answer_sha256 identifies the judged answer.
CREATE TABLE IF NOT EXISTS public.delegation_dispositions (
    tenant_id UUID NOT NULL,
    delegation_correlation_id UUID NOT NULL,
    disposition_id UUID NOT NULL,
    disposition TEXT NOT NULL CONSTRAINT delegation_dispositions_disposition_check
        CHECK (disposition IN ('accepted_as_is', 'edited', 'rejected', 'ignored')),
    reason_code TEXT NOT NULL,
    caller_lane TEXT NOT NULL,
    engine TEXT,
    artifact_kind TEXT NOT NULL CONSTRAINT delegation_dispositions_artifact_kind_check
        CHECK (artifact_kind IN ('pull_request', 'commit', 'document', 'none')),
    artifact_ref TEXT,
    edit_ratio DOUBLE PRECISION CONSTRAINT delegation_dispositions_edit_ratio_check
        CHECK (edit_ratio >= 0 AND edit_ratio <= 1),
    ticket_id TEXT,
    answer_sha256 TEXT,
    recorded_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, delegation_correlation_id)
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_dispositions ----
-- Guarded adds converge drifted tables without inventing required values.
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS delegation_correlation_id UUID;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS disposition_id UUID;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS disposition TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS reason_code TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS caller_lane TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS engine TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS artifact_kind TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS artifact_ref TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS edit_ratio DOUBLE PRECISION;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS answer_sha256 TEXT;
ALTER TABLE public.delegation_dispositions ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;

ALTER TABLE public.delegation_dispositions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN delegation_correlation_id SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN disposition_id SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN disposition SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN reason_code SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN caller_lane SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN artifact_kind SET NOT NULL;
ALTER TABLE public.delegation_dispositions ALTER COLUMN recorded_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_dispositions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_dispositions
            ADD CONSTRAINT delegation_dispositions_pkey
            PRIMARY KEY (tenant_id, delegation_correlation_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_dispositions'::regclass
          AND conname = 'delegation_dispositions_disposition_check'
    ) THEN
        ALTER TABLE public.delegation_dispositions
            ADD CONSTRAINT delegation_dispositions_disposition_check
            CHECK (disposition IN ('accepted_as_is', 'edited', 'rejected', 'ignored'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_dispositions'::regclass
          AND conname = 'delegation_dispositions_artifact_kind_check'
    ) THEN
        ALTER TABLE public.delegation_dispositions
            ADD CONSTRAINT delegation_dispositions_artifact_kind_check
            CHECK (artifact_kind IN ('pull_request', 'commit', 'document', 'none'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_dispositions'::regclass
          AND conname = 'delegation_dispositions_edit_ratio_check'
    ) THEN
        ALTER TABLE public.delegation_dispositions
            ADD CONSTRAINT delegation_dispositions_edit_ratio_check
            CHECK (edit_ratio >= 0 AND edit_ratio <= 1);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_dispositions ----

CREATE INDEX IF NOT EXISTS idx_delegation_dispositions_tenant_lane
    ON public.delegation_dispositions (tenant_id, caller_lane);
CREATE INDEX IF NOT EXISTS idx_delegation_dispositions_tenant_disposition
    ON public.delegation_dispositions (tenant_id, disposition);

ALTER TABLE public.delegation_dispositions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_dispositions;
CREATE POLICY tenant_isolation ON public.delegation_dispositions
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_dispositions TO app_dashboard;
