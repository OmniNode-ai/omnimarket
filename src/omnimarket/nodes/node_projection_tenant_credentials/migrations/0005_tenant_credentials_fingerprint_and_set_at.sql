-- OMN-19985: a credential row carries a fingerprint prefix and the time the key was set.
--
-- A locally set provider key (`onex secret set llm.<provider>.<field>`) now
-- emits credential-registered with `fingerprint` (the first 8 hex characters of
-- sha256 of the key, enough to tell two keys apart and never enough to recover
-- one) and `set_at` (when the user set it). The Credentials page shows both.
--
-- Both columns are nullable with no default and no backfill: a row registered
-- before this migration, a hosted registration (whose producer does not send
-- them) and a revoke-first tombstone have no fingerprint and no set time, and
-- inventing either would show a value nobody measured.
--
-- Numbered 0005 because omnibase_infra already vendors 0002, 003 and 004 for
-- this node; a lower number would collide there.
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS fingerprint TEXT;
ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS set_at TIMESTAMPTZ;
