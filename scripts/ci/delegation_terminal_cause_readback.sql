-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- Read-only acceptance probe. Pass -v deploy_boundary=<writer-deploy-time>
-- and -v ON_ERROR_STOP=1 to psql. Empty failure windows must fail too.
WITH counts AS (
    SELECT
        count(*) AS rows_after_deploy,
        count(*) FILTER (WHERE terminal_ok = false) AS failed_rows,
        count(*) FILTER (
            WHERE terminal_ok = false AND terminal_failure_cause IS NULL
        ) AS failed_null_cause,
        min(written_at) AS first_write,
        max(written_at) AS last_write
    FROM delegation_events
    WHERE written_at >= :'deploy_boundary'::timestamptz
)
SELECT
    counts.*,
    -- ON_ERROR_STOP turns a false or vacuous assertion into a nonzero exit.
    1 / (failed_rows > 0 AND failed_null_cause = 0)::integer AS acceptance
FROM counts;
