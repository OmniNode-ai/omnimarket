-- OMN-11299: repair existing savings_estimates projections missing updated_at.

ALTER TABLE savings_estimates
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
