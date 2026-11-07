-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-21970: the SQLite counterpart of node_projection_delegation migration
-- 0066's projection_delegation_daily view: one row per tenant whose "days" list
-- holds every UTC day with runs, oldest first, so the Overview's Runs per day
-- and Tokens per day count every run instead of the newest 500 rows. One row
-- per tenant is what the writer's singleton-aggregate republish can serve.
-- Same columns, names and rules as migration 0066:
-- * day is the UTC date of written_at, else created_at, as the dashboard
--   places a run;
-- * a run whose terminal was not ok failed whatever its quality gate said;
--   otherwise the quality gate decides; a run with neither is unrecorded;
-- * a token sum is null when no run that day carries the count, and the
--   *Unmeasured fields count the runs without it;
-- * "days" is json_group_array text; the exposure declares it a json_column,
--   so the read decodes it to the list jsonb_agg returns.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step
-- omn21970_delegation_daily_view; a revision is a new step, never an edit here.
CREATE VIEW projection_delegation_daily AS
WITH per_day AS (
    SELECT
        tenant_id,
        date(COALESCE(written_at, created_at)) AS day,
        COUNT(*) AS runs,
        SUM(CASE WHEN terminal_ok IS NOT 0 AND quality_gate_passed = 1
            THEN 1 ELSE 0 END) AS passed,
        SUM(CASE WHEN terminal_ok = 0 OR quality_gate_passed = 0
            THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN terminal_ok IS NOT 0 AND quality_gate_passed IS NULL
            THEN 1 ELSE 0 END) AS unrecorded,
        SUM(tokens_input) AS tokens_input,
        SUM(tokens_output) AS tokens_output,
        SUM(CASE WHEN tokens_input IS NULL THEN 1 ELSE 0 END) AS tokens_input_unmeasured,
        SUM(CASE WHEN tokens_output IS NULL THEN 1 ELSE 0 END) AS tokens_output_unmeasured,
        MAX(COALESCE(written_at, created_at)) AS latest_written_at
    FROM delegation_events
    GROUP BY tenant_id, date(COALESCE(written_at, created_at))
    ORDER BY tenant_id, day
)
SELECT
    tenant_id,
    json_group_array(json_object(
        'day', day,
        'runs', runs,
        'passed', passed,
        'failed', failed,
        'unrecorded', unrecorded,
        'tokensIn', tokens_input,
        'tokensOut', tokens_output,
        'tokensInUnmeasured', tokens_input_unmeasured,
        'tokensOutUnmeasured', tokens_output_unmeasured
    )) AS days,
    SUM(runs) AS total_runs,
    MAX(latest_written_at) AS latest_projection_updated_at
FROM per_day
GROUP BY tenant_id;
