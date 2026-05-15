-- OMN-11042: Create the dep_health_findings projection table.

CREATE TABLE IF NOT EXISTS dep_health_findings (
  id BIGSERIAL PRIMARY KEY,
  run_id VARCHAR NOT NULL,
  finding_type VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  repo VARCHAR NOT NULL,
  file_path VARCHAR NOT NULL DEFAULT '',
  symbol VARCHAR NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  rule_id VARCHAR NOT NULL,
  rule_version VARCHAR NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL,
  UNIQUE (run_id, finding_type, file_path, symbol)
);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_run_id
  ON dep_health_findings (run_id);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_severity
  ON dep_health_findings (severity);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_repo
  ON dep_health_findings (repo);

CREATE INDEX IF NOT EXISTS idx_dep_health_findings_captured_at
  ON dep_health_findings (captured_at);
