-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-24615 AC7: the revision of delegation_daily_view.sql (OMN-21970). Its unrecorded and *Unmeasured counts
-- tested IS NULL on columns the local store declares NOT NULL DEFAULT 0, so they were 0 on every day (an
-- operator's store copy, 2026-11-15: 168 runs never evaluated, 175 runs with no tokens recorded). Changes, nothing
-- else, using the store's own markers, the rules metering-summary.v1 already uses:
-- * a run whose quality was never evaluated (quality_gates_checked = 0) is unrecorded, not failed, unless its
--   terminal was not ok;
-- * a run with both token counts 0 (or NULL) is unmeasured, and its zeros are not summed as measured tokens.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step omn24615_daily_store_markers;
-- a revision is a new step, never an edit here.
CREATE VIEW projection_delegation_daily AS
WITH per_day AS (
    SELECT
        tenant_id,
        date(COALESCE(written_at, created_at)) AS day,
        COUNT(*) AS runs,
        SUM(CASE WHEN terminal_ok IS NOT 0 AND quality_gates_checked IS NOT 0 AND quality_gate_passed = 1
            THEN 1 ELSE 0 END) AS passed,
        SUM(CASE WHEN terminal_ok = 0
            OR (quality_gates_checked IS NOT 0 AND quality_gate_passed = 0)
            THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN terminal_ok IS NOT 0
            AND (quality_gates_checked = 0 OR quality_gate_passed IS NULL)
            THEN 1 ELSE 0 END) AS unrecorded,
        SUM(CASE WHEN NOT (COALESCE(tokens_input, 0) = 0 AND COALESCE(tokens_output, 0) = 0)
            THEN tokens_input END) AS tokens_input,
        SUM(CASE WHEN NOT (COALESCE(tokens_input, 0) = 0 AND COALESCE(tokens_output, 0) = 0)
            THEN tokens_output END) AS tokens_output,
        SUM(CASE WHEN tokens_input IS NULL
            OR (COALESCE(tokens_input, 0) = 0 AND COALESCE(tokens_output, 0) = 0)
            THEN 1 ELSE 0 END) AS tokens_input_unmeasured,
        SUM(CASE WHEN tokens_output IS NULL
            OR (COALESCE(tokens_input, 0) = 0 AND COALESCE(tokens_output, 0) = 0)
            THEN 1 ELSE 0 END) AS tokens_output_unmeasured,
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
